"""Agent and model scoring, weight auto-tuning, leaderboard (SPEC §7.7, §12).

Score = Spearman correlation (IC) between an agent's score and the forward return of the
asset over 4 h, 1 d and 3 d, over every stored cycle. Models are compared the same way on
their proposals' signed direction × conviction (main vs alt variants of each PM).

Weight tuning every 2 weeks, only after ≥ 100 closed primary trades: each agent's weight
moves at most ±5 pp toward the IC ranking, then shrinks halfway toward equal weights, then
renormalizes. An agent whose IC is ≤ 0 is never zeroed in one go: it loses 5 pp per step,
and only while its IC was negative in each of the last two tuning windows; until then its
weight stands. Stored in ``agent_weights``; the formula anchor reads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config
from app.db.models import AgentOutputRecord, AgentWeights, Candle, PMProposalRecord, Position

HORIZONS = {"4h": 4, "1d": 24, "3d": 72}
MIN_SAMPLE = 30
MAX_STEP = 0.05
LOOKBACK_DAYS = 90  # the IC the ranking uses
WINDOW_DAYS = 14  # one tuning window; a penalty needs two negative ones in a row
BASE_WEIGHTS = {
    "indicators": 0.25,
    "chart_patterns": 0.20,
    "polymarket": 0.20,
    "macro": 0.20,
    "x_sentiment": 0.15,
}


@dataclass(frozen=True)
class Score:
    name: str
    kind: str  # agent | model
    ic: dict[str, float | None]
    sample: int


def _closes(session: Session, cfg: Config, asset: str, since: datetime) -> pd.Series:
    rows = session.execute(
        select(Candle.open_time, Candle.close)
        .where(
            Candle.symbol == cfg.symbol(asset),
            Candle.interval == "60",
            Candle.open_time >= since - timedelta(hours=4),
        )
        .order_by(Candle.open_time)
    ).all()
    return pd.Series(
        [float(r.close) for r in rows], index=pd.DatetimeIndex([r.open_time for r in rows])
    )


def forward_return(closes: pd.Series, at: datetime, hours: int) -> float | None:
    if closes.empty:
        return None
    start = closes.index.searchsorted(pd.Timestamp(at))
    end = closes.index.searchsorted(pd.Timestamp(at + timedelta(hours=hours)))
    if start >= len(closes) or end >= len(closes):
        return None
    return float(closes.iloc[end] / closes.iloc[start] - 1)


def _ic(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < MIN_SAMPLE:
        return None
    df = pd.DataFrame(pairs, columns=["s", "r"])
    return float(df["s"].rank().corr(df["r"].rank()))


def score_agents(
    session: Session, cfg: Config, since: datetime, until: datetime | None = None
) -> list[Score]:
    stmt = select(AgentOutputRecord).where(
        AgentOutputRecord.computed_at >= since,
        AgentOutputRecord.valid.is_(True),
        AgentOutputRecord.variant == "main",
    )
    if until is not None:
        stmt = stmt.where(AgentOutputRecord.computed_at < until)
    outs = session.scalars(stmt).all()
    closes = {a: _closes(session, cfg, a, since) for a in cfg.trading.assets}
    by_agent: dict[str, dict[str, list[tuple[float, float]]]] = {}
    for o in outs:
        for h, hours in HORIZONS.items():
            r = forward_return(closes.get(o.asset, pd.Series(dtype=float)), o.computed_at, hours)
            if r is not None:
                by_agent.setdefault(o.agent, {}).setdefault(h, []).append((o.score, r))
    return [
        Score(
            agent,
            "agent",
            {h: _ic(by_agent[agent].get(h, [])) for h in HORIZONS},
            len(by_agent[agent].get("1d", [])),
        )
        for agent in sorted(by_agent)
    ]


def score_models(session: Session, cfg: Config, since: datetime) -> list[Score]:
    props = session.execute(
        select(PMProposalRecord, AgentOutputRecord.computed_at)
        .join(
            AgentOutputRecord,
            (AgentOutputRecord.cycle_id == PMProposalRecord.cycle_id)
            & (AgentOutputRecord.asset == PMProposalRecord.asset)
            & (AgentOutputRecord.agent == "indicators"),
        )
        .where(AgentOutputRecord.computed_at >= since)
    ).all()
    closes = {a: _closes(session, cfg, a, since) for a in cfg.trading.assets}
    by_model: dict[str, dict[str, list[tuple[float, float]]]] = {}
    for p, at in props:
        signed = {"long": 1.0, "short": -1.0, "flat": 0.0}[p.direction] * p.conviction
        for h, hours in HORIZONS.items():
            r = forward_return(closes.get(p.asset, pd.Series(dtype=float)), at, hours)
            if r is not None:
                by_model.setdefault(f"{p.model} ({p.pm}/{p.variant})", {}).setdefault(h, []).append(
                    (signed, r)
                )
    return [
        Score(
            name,
            "model",
            {h: _ic(by_model[name].get(h, [])) for h in HORIZONS},
            len(by_model[name].get("1d", [])),
        )
        for name in sorted(by_model)
    ]


def current_weights(session: Session) -> dict[str, float]:
    row = session.execute(
        select(AgentWeights).order_by(AgentWeights.id.desc()).limit(1)
    ).scalar_one_or_none()
    return dict(row.weights) if row else dict(BASE_WEIGHTS)


def tune_step(
    weights: dict[str, float], ics: dict[str, float | None], negative_twice: set[str]
) -> dict[str, float]:
    """One tuning step as arithmetic.

    ``ics``: each agent's IC over the lookback (None = too few samples: weight stands).
    ``negative_twice``: agents whose IC was negative in both of the last two windows.
    Positive IC: at most ±5 pp toward the ranking. IC ≤ 0: −5 pp when in
    ``negative_twice``, otherwise unchanged; never straight to zero. Then shrink halfway
    toward equal weights and renormalize.
    """
    valid = {a: ic for a, ic in ics.items() if ic is not None and a in weights}
    mean_ic = sum(valid.values()) / len(valid) if valid else 0.0
    new: dict[str, float] = {}
    for a, w in weights.items():
        ic = valid.get(a)
        if ic is None:
            step = 0.0
        elif ic <= 0:
            step = -MAX_STEP if a in negative_twice else 0.0
        else:
            step = max(-MAX_STEP, min(MAX_STEP, ic - mean_ic))  # move toward the ranking
        new[a] = max(0.0, w + step)
    equal = 1.0 / len(new)
    new = {a: (w + equal) / 2 for a, w in new.items()}  # shrink halfway toward equal
    total = sum(new.values()) or 1.0
    return {a: round(w / total, 4) for a, w in new.items()}


def tune(session: Session, cfg: Config, now: datetime | None = None) -> dict[str, float] | None:
    """One tuning step; None when the trade count is too low. Pure arithmetic once scored."""
    now = now or datetime.now(UTC)
    closed = session.execute(
        select(Position.id).where(Position.track == "primary", Position.status == "closed")
    ).all()
    if len(closed) < 100:
        return None
    scores = {s.name: s for s in score_agents(session, cfg, now - timedelta(days=LOOKBACK_DAYS))}
    weights = current_weights(session)
    ics = {a: (scores[a].ic.get("1d") if a in scores else None) for a in weights}
    if sum(ic is not None for ic in ics.values()) < 2:
        return None
    window = timedelta(days=WINDOW_DAYS)
    last = {s.name: s.ic.get("1d") for s in score_agents(session, cfg, now - window, now)}
    prev = {
        s.name: s.ic.get("1d") for s in score_agents(session, cfg, now - 2 * window, now - window)
    }
    negative_twice = {a for a in weights if (last.get(a) or 0.0) < 0 and (prev.get(a) or 0.0) < 0}
    new = tune_step(weights, ics, negative_twice)
    basis: dict[str, object] = {a: scores[a].__dict__ for a in scores}
    basis["windows_1d"] = {"last": last, "previous": prev, "penalized": sorted(negative_twice)}
    session.add(AgentWeights(ts=now, weights=new, basis=basis))
    session.commit()
    return new


def leaderboard(session: Session, cfg: Config, since: datetime) -> list[Score]:
    return score_agents(session, cfg, since) + score_models(session, cfg, since)
