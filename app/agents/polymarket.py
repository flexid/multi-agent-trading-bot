"""Polymarket agent (SPEC §6): implied distribution from threshold ladders, 24h shift first.

Code does the scoring. The model is only used once a day to map new markets to an asset
and a threshold (``app/agents/polymarket_map.py``); its output is validated and stored on
``polymarket_markets.asset`` / ``mapping`` and never re-read as free text.

Scoring per asset at time t, over mapped markets that pass the filters:

- for each "above X" market, p = P(yes); for each "dip to X" (below) market, p = 1 − P(yes)
  gives P(price ≥ X); the set of (X, P) is the implied ladder
- level: the probability-weighted expected position of price relative to spot,
  mapped to [-1, 1] by the ladder's spread; this is the slow component
- shift: the volume-weighted mean 24h change of P(price ≥ X) across markets, scaled so a
  10-percentage-point shift is ±1; this is the fast component and weighs 2:1 over level
- confidence from market count and liquidity; BNB/SPX6900 get few or no markets, so
  confidence stays low there, which is how the agent "weighs less" (SPEC §6)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.schema import AgentOutput, Horizon
from app.db.models import PolymarketMarket, PolymarketPrice

AGENT = "polymarket"
MIN_VOLUME_24H = Decimal(1000)  # USD; thinner markets are noise
MIN_HOURS_TO_RESOLUTION = 1
MAX_DAYS_TO_RESOLUTION = 60  # year-end markets carry little 4h-to-3d information
SHIFT_FULL_SCALE = 0.10  # 10 pp move in 24h = ±1
WEIGHT_SHIFT, WEIGHT_LEVEL = 2 / 3, 1 / 3


@dataclass(frozen=True)
class LadderPoint:
    market_id: str
    threshold: float
    p_above: float  # P(price ≥ threshold) now
    p_above_24h: float | None
    volume_24h: float
    hours_to_resolution: float


def _p_above(direction: str, p_yes: float) -> float:
    return p_yes if direction == "above" else 1 - p_yes


def ladder(session: Session, asset: str, now: datetime) -> list[LadderPoint]:
    """Mapped, open, liquid markets for ``asset`` with their latest and 24h-ago prices."""
    markets = session.scalars(
        select(PolymarketMarket).where(
            PolymarketMarket.asset == asset,
            PolymarketMarket.closed.is_(False),
            PolymarketMarket.mapping.is_not(None),
        )
    ).all()
    points: list[LadderPoint] = []
    for m in markets:
        mapping: dict[str, Any] = m.mapping or {}
        threshold, direction = mapping.get("threshold"), mapping.get("direction")
        if not threshold or direction not in ("above", "below") or m.end_date is None:
            continue
        hours = (m.end_date - now).total_seconds() / 3600
        if hours < MIN_HOURS_TO_RESOLUTION or hours > MAX_DAYS_TO_RESOLUTION * 24:
            continue
        latest = _price_at(session, m.id, now)
        if latest is None or latest[1] < MIN_VOLUME_24H:
            continue
        earlier = _price_at(session, m.id, now - timedelta(hours=24))
        yes_index = next((i for i, o in enumerate(m.outcomes) if str(o).lower() == "yes"), 0)
        p_now = _p_above(direction, float(latest[0][yes_index]))
        p_prev = (
            _p_above(direction, float(earlier[0][yes_index]))
            if earlier is not None and earlier[2] <= now - timedelta(hours=20)
            else None
        )
        points.append(LadderPoint(m.id, float(threshold), p_now, p_prev, float(latest[1]), hours))
    return points


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


def evaluate(
    session: Session, asset: str, spot: float, data_age_min: int, now: datetime | None = None
) -> AgentOutput:
    now = now or datetime.now(UTC)
    points = ladder(session, asset, now)
    score, level, shift, evidence = score_ladder(points, spot)
    n = len(points)
    liquidity = sum(p.volume_24h for p in points)
    confidence = min(1.0, 0.15 * n) * min(1.0, liquidity / 50_000)
    if not any(p.p_above_24h is not None for p in points):
        confidence *= 0.5  # no 24h history yet: only the level is known
    risk_flags = []
    if n == 0:
        evidence = ["no mapped Polymarket markets for this asset"]
        risk_flags.append("no polymarket coverage")
    else:
        evidence.insert(0, f"{n} markets, level {level:+.2f}, 24h shift {shift:+.2f}")
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
