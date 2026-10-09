"""Polymarket agent (SPEC §6): implied distribution from threshold ladders, 24h shift first,
plus the Up/Down markets as a momentum read.

Code does the scoring. Question text is mapped to an asset and a threshold by the parser
or, for the few questions it cannot read, by the model (``app/agents/polymarket_map.py``);
the mapping is validated and stored on ``polymarket_markets.asset`` / ``mapping`` and never
re-read as free text.

Scoring per asset at time t, over mapped markets that pass the filters:

- for each "above X" market, p = P(yes); for each "dip to X" (below) market, p = 1 − P(yes)
  gives P(price ≥ X); the set of (X, P) is the implied ladder
- level: the probability-weighted expected position of price relative to spot,
  mapped to [-1, 1] by the ladder's spread; this is the slow component
- shift: the volume-weighted mean 24h change of P(price ≥ X) across markets, scaled so a
  10-percentage-point shift is ±1; this is the fast component and weighs 2:1 over level
- updown: the volume-weighted P(up) of the hourly, 4-hour and daily Up/Down markets,
  centred and scaled so 65% is +1; 5- and 15-minute markets are never stored
- confidence from market count and liquidity; BNB/SPX6900 get few or no markets, so
  confidence stays low there, which is how the agent "weighs less" (SPEC §6)

``coverage`` tells "no markets at all" from "markets, none usable" with the reasons, for
the agent's evidence and the admin health page.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.polymarket_parse import MIN_UPDOWN_WINDOW_MIN
from app.agents.schema import AgentOutput, Horizon
from app.db.models import PolymarketMarket, PolymarketPrice

AGENT = "polymarket"
MIN_VOLUME_24H = Decimal(1000)  # USD; thinner markets are noise
MIN_HOURS_TO_RESOLUTION = 1
MAX_DAYS_TO_RESOLUTION = 60  # year-end markets carry little 4h-to-3d information
SHIFT_FULL_SCALE = 0.10  # 10 pp move in 24h = ±1
UPDOWN_FULL_SCALE = 0.15  # P(up) 65% = +1, 35% = −1
WEIGHT_SHIFT, WEIGHT_LEVEL, WEIGHT_UPDOWN = 0.5, 0.25, 0.25


@dataclass(frozen=True)
class LadderPoint:
    market_id: str
    threshold: float
    p_above: float  # P(price ≥ threshold) now
    p_above_24h: float | None
    volume_24h: float
    hours_to_resolution: float


@dataclass(frozen=True)
class UpDownPoint:
    market_id: str
    p_up: float
    window_min: int
    hours_to_resolution: float
    volume_24h: float


@dataclass
class Coverage:
    asset: str
    open_markets: int = 0  # mapped to this asset and not closed
    unmapped: int = 0  # open markets nobody has mapped yet (parser leftovers, model pending)
    ladder: int = 0  # usable threshold points
    updown: int = 0  # usable Up/Down markets
    dropped: Counter[str] = field(default_factory=Counter)

    @property
    def usable(self) -> int:
        return self.ladder + self.updown

    @property
    def status(self) -> str:
        if self.open_markets == 0:
            return "no markets"
        if self.usable == 0:
            why = ", ".join(f"{n} {k}" for k, n in self.dropped.most_common(3)) or "unknown"
            return f"{self.open_markets} markets, none usable ({why})"
        return f"ok: {self.ladder} ladder points, {self.updown} up/down"


def _p_above(direction: str, p_yes: float) -> float:
    return p_yes if direction == "above" else 1 - p_yes


def _outcome_index(outcomes: list[Any], name: str) -> int:
    return next((i for i, o in enumerate(outcomes) if str(o).lower() == name), 0)


def _price_at(
    session: Session, market_id: str, at: datetime
) -> tuple[list[Decimal], Decimal, datetime] | None:
    row = session.execute(
        select(PolymarketPrice.prices, PolymarketPrice.volume_24h, PolymarketPrice.ts)
        .where(PolymarketPrice.market_id == market_id, PolymarketPrice.ts <= at)
        .order_by(PolymarketPrice.ts.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return [Decimal(str(p)) for p in row[0]], Decimal(str(row[1])), row[2]


def _points(
    session: Session, asset: str, now: datetime, cov: Coverage
) -> tuple[list[LadderPoint], list[UpDownPoint]]:
    """Mapped, open, liquid markets for ``asset`` as ladder and Up/Down points, counting
    every drop reason into ``cov``."""
    markets = session.scalars(
        select(PolymarketMarket).where(
            PolymarketMarket.asset == asset, PolymarketMarket.closed.is_(False)
        )
    ).all()
    ladder: list[LadderPoint] = []
    updown: list[UpDownPoint] = []
    for m in markets:
        mapping: dict[str, Any] = m.mapping or {}
        kind = mapping.get("kind")
        if kind is None:
            cov.unmapped += 1
            continue
        cov.open_markets += 1
        if kind == "range":
            cov.dropped["range market"] += 1
            continue
        if kind == "other":
            cov.dropped["not a price market"] += 1
            continue
        if m.end_date is None:
            cov.dropped["no end date"] += 1
            continue
        hours = (m.end_date - now).total_seconds() / 3600
        if kind == "updown":
            window = int(mapping.get("window_min") or 0)
            if window < MIN_UPDOWN_WINDOW_MIN:
                cov.dropped["window under an hour"] += 1
                continue
            if hours <= 0:
                cov.dropped["resolved"] += 1
                continue
        elif kind == "price":
            threshold = float(mapping.get("threshold") or 0)
            direction = str(mapping.get("direction") or "")
            if threshold <= 0 or direction not in ("above", "below"):
                cov.dropped["bad mapping"] += 1
                continue
            if hours < MIN_HOURS_TO_RESOLUTION:
                cov.dropped["resolves within the hour"] += 1
                continue
            if hours > MAX_DAYS_TO_RESOLUTION * 24:
                cov.dropped["resolves beyond 60 days"] += 1
                continue
        else:
            cov.dropped["bad mapping"] += 1
            continue
        latest = _price_at(session, m.id, now)
        if latest is None:
            cov.dropped["no price yet"] += 1
            continue
        if latest[1] < MIN_VOLUME_24H:
            cov.dropped["24h volume under $1,000"] += 1
            continue
        if kind == "updown":
            p_up = float(latest[0][_outcome_index(m.outcomes, "up")])
            updown.append(UpDownPoint(m.id, p_up, window, hours, float(latest[1])))
            continue
        earlier = _price_at(session, m.id, now - timedelta(hours=24))
        yes_index = _outcome_index(m.outcomes, "yes")
        p_now = _p_above(direction, float(latest[0][yes_index]))
        p_prev = (
            _p_above(direction, float(earlier[0][yes_index]))
            if earlier is not None and earlier[2] <= now - timedelta(hours=20)
            else None
        )
        ladder.append(LadderPoint(m.id, threshold, p_now, p_prev, float(latest[1]), hours))
    cov.ladder, cov.updown = len(ladder), len(updown)
    return ladder, updown


def ladder(session: Session, asset: str, now: datetime) -> list[LadderPoint]:
    return _points(session, asset, now, Coverage(asset))[0]


def coverage(session: Session, asset: str, now: datetime | None = None) -> Coverage:
    cov = Coverage(asset)
    _points(session, asset, now or datetime.now(UTC), cov)
    return cov


def implied_median(points: list[LadderPoint]) -> float:
    """Price where the ladder's P(price ≥ X) crosses 50%, interpolated in log space.

    Noisy ladders can cross more than once; the crossings are volume-weighted. If every
    market sits on one side of 50% the median is beyond the ladder's edge, so the edge is
    returned and the caller's width clip does the rest.
    """
    ordered = sorted(points, key=lambda p: p.threshold)
    crossings: list[tuple[float, float]] = []
    for lo, hi in zip(ordered, ordered[1:], strict=False):
        if (lo.p_above - 0.5) * (hi.p_above - 0.5) < 0:
            t = (lo.p_above - 0.5) / (lo.p_above - hi.p_above)
            x = math.exp(math.log(lo.threshold) + t * math.log(hi.threshold / lo.threshold))
            crossings.append((x, max(lo.volume_24h, 1.0) + max(hi.volume_24h, 1.0)))
    if crossings:
        return sum(x * w for x, w in crossings) / sum(w for _, w in crossings)
    return ordered[-1].threshold if ordered[-1].p_above >= 0.5 else ordered[0].threshold


def score_ladder(points: list[LadderPoint], spot: float) -> tuple[float, float, float, list[str]]:
    """Return (score, level, shift, evidence)."""
    if not points or spot <= 0:
        return 0.0, 0.0, 0.0, []
    weights = [max(p.volume_24h, 1.0) for p in points]
    width = max(abs(math.log(p.threshold / spot)) for p in points) or 1.0
    level = max(-1.0, min(1.0, math.log(implied_median(points) / spot) / width))

    shifted = [
        (w, p.p_above - p.p_above_24h)
        for w, p in zip(weights, points, strict=True)
        if p.p_above_24h is not None
    ]
    if shifted:
        shift_raw = sum(w * d for w, d in shifted) / sum(w for w, _ in shifted)
        shift = max(-1.0, min(1.0, shift_raw / SHIFT_FULL_SCALE))
    else:
        shift = 0.0
    score = WEIGHT_SHIFT * shift + WEIGHT_LEVEL * level if shifted else level
    if shifted:
        score /= WEIGHT_SHIFT + WEIGHT_LEVEL

    top = sorted(points, key=lambda p: -p.volume_24h)[:3]
    evidence = [
        f"P(≥{p.threshold:g}) {p.p_above:.0%}"
        + (
            f" ({(p.p_above - p.p_above_24h) * 100:+.0f} pp 24h)"
            if p.p_above_24h is not None
            else ""
        )
        + f", resolves in {p.hours_to_resolution / 24:.1f} d, ${p.volume_24h:,.0f}/24h"
        for p in top
    ]
    return score, level, shift, evidence


def score_updown(points: list[UpDownPoint]) -> tuple[float, list[str]]:
    """Volume-weighted P(up) over the open Up/Down windows, centred on 50%."""
    if not points:
        return 0.0, []
    weights = [max(p.volume_24h, 1.0) for p in points]
    p_up = sum(w * p.p_up for w, p in zip(weights, points, strict=True)) / sum(weights)
    score = max(-1.0, min(1.0, (p_up - 0.5) / UPDOWN_FULL_SCALE))
    top = sorted(points, key=lambda p: -p.volume_24h)[:2]
    evidence = [
        f"P(up) {p.p_up:.0%} over {_window_text(p.window_min)}, "
        f"resolves in {p.hours_to_resolution:.1f} h, ${p.volume_24h:,.0f}/24h"
        for p in top
    ]
    return score, evidence


def _window_text(minutes: int) -> str:
    if minutes % (24 * 60) == 0:
        return f"{minutes // (24 * 60)} d"
    if minutes % 60 == 0:
        return f"{minutes // 60} h"
    return f"{minutes} min"


def combine(*, level: float, shift: float | None, updown: float | None) -> float:
    """Weighted sum over the components that exist, renormalized to the weights present."""
    parts = [(WEIGHT_LEVEL, level)]
    if shift is not None:
        parts.append((WEIGHT_SHIFT, shift))
    if updown is not None:
        parts.append((WEIGHT_UPDOWN, updown))
    total = sum(w for w, _ in parts)
    return sum(w * v for w, v in parts) / total


def evaluate(
    session: Session, asset: str, spot: float, data_age_min: int, now: datetime | None = None
) -> AgentOutput:
    now = now or datetime.now(UTC)
    cov = Coverage(asset)
    points, ud_points = _points(session, asset, now, cov)
    _, level, shift, evidence = score_ladder(points, spot)
    has_shift = any(p.p_above_24h is not None for p in points)
    ud_score, ud_evidence = score_updown(ud_points)
    score = combine(
        level=level if points else 0.0,
        shift=shift if has_shift else None,
        updown=ud_score if ud_points else None,
    )
    n = len(points) + len(ud_points)
    liquidity = sum(p.volume_24h for p in points) + sum(p.volume_24h for p in ud_points)
    confidence = min(1.0, 0.15 * n) * min(1.0, liquidity / 50_000)
    if not has_shift:
        confidence *= 0.5  # no 24h history yet: only the level and the momentum are known
    risk_flags = []
    if n == 0:
        evidence = [
            "no Polymarket markets for this asset"
            if cov.open_markets == 0
            else f"Polymarket: {cov.status}"
        ]
        risk_flags.append(
            "no polymarket coverage" if cov.open_markets == 0 else "polymarket: none usable"
        )
    else:
        evidence = [
            f"{len(points)} ladder points, {len(ud_points)} up/down, level {level:+.2f}, "
            f"24h shift {shift:+.2f}, up/down {ud_score:+.2f}",
            *evidence,
            *ud_evidence,
        ]
    return AgentOutput(
        agent=AGENT,
        asset=asset,
        score=score,
        confidence=confidence,
        horizon=Horizon.D1_3,
        evidence=evidence[:10],
        risk_flags=risk_flags,
        data_age_min=data_age_min,
    )
