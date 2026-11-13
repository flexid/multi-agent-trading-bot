"""Skill against drift: the scoreboard for anyone who calls a direction.

A call's forward return says little by itself: in a month in which everything fell a
fifth, every short "worked". What a caller adds is the difference between the return
after its calls and the return the same calls would have earned at an average moment
(the drift): positive means the timing helped, negative means a rule that is simply
always on that side did better. The review of 2026-11-12 (docs/RETHINK_2026-11.md) found
every manager seat negative at every horizon; the weekly memo reports this table and the
challenger's promotion rule is written in it.

Forward returns are measured on 15-minute closes, from the last close at the decision's
moment to the last close that many hours later. (Not from the decision's own ``spot``:
that is the last closed four-hour candle, up to four hours old on a triggered cycle, and
a cycle is triggered because the price just moved, so the move that woke the managers
would be booked as their forecast.) The drift per asset is the mean forward return over
every decision moment in the window, whatever was called then.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config
from app.db.models import Candle, DecisionRecord

HORIZONS = {"1d": 24, "3d": 72, "7d": 168}
CANDLE = timedelta(minutes=15)
SEATS = (
    ("consensus", "direction"),
    ("pm_1", "pm1_direction"),
    ("pm_2", "pm2_direction"),
    ("pm_3", "pm3_direction"),
)


@dataclass(frozen=True)
class Horizon:
    n: int  # calls with a price that far ahead
    mean_pct: float | None  # the return after the calls, in the direction called
    drift_pct: float | None  # the same calls at an average moment
    skill_pct: float | None  # mean minus drift
    right: float | None  # share of calls with a positive return


@dataclass(frozen=True)
class Skill:
    who: str
    calls: int
    days: int  # distinct UTC days with at least one call
    short_share: float | None
    horizons: dict[str, Horizon] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "who": self.who,
            "calls": self.calls,
            "days": self.days,
            "short_share": self.short_share,
            **{h: v.__dict__ for h, v in self.horizons.items()},
        }


class Closes:
    """The 15-minute closes of one symbol; ``at`` is the last close at or before a time."""

    def __init__(self, rows: list[tuple[datetime, float]]) -> None:
        self.t = [t + CANDLE for t, _ in rows]  # the time the candle closed
        self.c = [c for _, c in rows]

    def at(self, when: datetime) -> float | None:
        if not self.t or when > self.t[-1]:
            return None  # not there yet: no forward return
        i = bisect_right(self.t, when) - 1
        return self.c[i] if i >= 0 else None


def closes(session: Session, symbol: str, since: datetime) -> Closes:
    rows = session.execute(
        select(Candle.open_time, Candle.close)
        .where(Candle.symbol == symbol, Candle.interval == "15", Candle.open_time >= since - CANDLE)
        .order_by(Candle.open_time)
    ).all()
    return Closes([(t, float(c)) for t, c in rows])


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _pct(x: float | None) -> float | None:
    return round(x * 100, 3) if x is not None else None


def skill_table(
    session: Session, cfg: Config, since: datetime, until: datetime | None = None
) -> list[Skill]:
    """One row for the two-of-three consensus and one per seat, over the decisions between
    ``since`` and ``until`` on the traded assets."""
    q = select(DecisionRecord).where(
        DecisionRecord.ts >= since,
        DecisionRecord.asset.in_(cfg.trading.assets),
        DecisionRecord.spot > 0,
    )
    if until is not None:
        q = q.where(DecisionRecord.ts < until)
    decisions = list(session.scalars(q.order_by(DecisionRecord.ts)).all())
    price = {a: closes(session, cfg.symbol(a), since) for a in cfg.trading.assets}
    # forward return of the asset after every decision moment, per horizon
    fwd: dict[int, dict[str, float | None]] = {}
    for d in decisions:
        # the price when the call was made; the decision's own spot only without candles
        start = price[d.asset].at(d.ts) or float(d.spot or 0)
        row: dict[str, float | None] = {}
        for h, hours in HORIZONS.items():
            later = price[d.asset].at(d.ts + timedelta(hours=hours))
            row[h] = later / start - 1 if later is not None and start > 0 else None
        fwd[d.id] = row
    drift: dict[tuple[str, str], float | None] = {}
    for a in cfg.trading.assets:
        for h in HORIZONS:
            drift[(a, h)] = _mean(
                [v for d in decisions if d.asset == a and (v := fwd[d.id][h]) is not None]
            )
    labels = {
        "consensus": "consensus (2 of 3)",
        "pm_1": f"pm_1 ({cfg.models.pm_1})",
        "pm_2": f"pm_2 ({cfg.models.pm_2})",
        "pm_3": f"pm_3 ({cfg.models.pm_3 or 'none'})",
    }
    out: list[Skill] = []
    for key, column in SEATS:
        calls = [d for d in decisions if getattr(d, column) in ("long", "short")]
        signs = {d.id: (1.0 if getattr(d, column) == "long" else -1.0) for d in calls}
        horizons: dict[str, Horizon] = {}
        for h in HORIZONS:
            got: list[float] = []
            expected: list[float] = []
            for d in calls:
                f, base = fwd[d.id][h], drift[(d.asset, h)]
                if f is None or base is None:
                    continue
                got.append(signs[d.id] * f)
                expected.append(signs[d.id] * base)
            mean, exp = _mean(got), _mean(expected)
            horizons[h] = Horizon(
                n=len(got),
                mean_pct=_pct(mean),
                drift_pct=_pct(exp),
                skill_pct=_pct(mean - exp) if mean is not None and exp is not None else None,
                right=round(sum(1 for x in got if x > 0) / len(got), 3) if got else None,
            )
        out.append(
            Skill(
                who=labels[key],
                calls=len(calls),
                days=len({d.ts.date() for d in calls}),
                short_share=round(sum(1 for s in signs.values() if s < 0) / len(calls), 3)
                if calls
                else None,
                horizons=horizons,
            )
        )
    return out
