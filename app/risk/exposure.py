"""Correlation-aware exposure (owner briefing 2026-10-10).

The five assets move largely as one: five same-direction positions are mostly one bet
on crypto. Each asset gets a 30-day beta to BTC from 4-hour returns (BTC = 1); the net
same-direction exposure is the signed sum of position notional × beta, and the risk
engine caps it at ``net_beta_exposure_max`` × equity (1.5 at the start). Logged per cycle
and shown in the admin.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config
from app.db.models import Candle, Position

BETA_DAYS = 30
MIN_POINTS = 10
ONE = Decimal(1)


def beta_from_returns(asset: list[float], btc: list[float]) -> Decimal:
    """Slope of the asset's returns on BTC's; 1 when there is too little data."""
    n = min(len(asset), len(btc))
    if n < MIN_POINTS:
        return ONE
    a, b = asset[-n:], btc[-n:]
    mb = sum(b) / n
    ma = sum(a) / n
    var = sum((x - mb) ** 2 for x in b)
    if var <= 0:
        return ONE
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    return Decimal(str(round(cov / var, 3)))


def _returns(
    session: Session, symbol: str, since: datetime, interval: str = "240"
) -> dict[datetime, float]:
    rows = session.execute(
        select(Candle.open_time, Candle.close)
        .where(Candle.symbol == symbol, Candle.interval == interval, Candle.open_time >= since)
        .order_by(Candle.open_time)
    ).all()
    out: dict[datetime, float] = {}
    prev: Decimal | None = None
    for r in rows:
        if prev and prev > 0:
            out[r.open_time] = float(r.close / prev - 1)
        prev = r.close
    return out


def _aligned(
    session: Session, cfg: Config, asset: str, days: int, now: datetime
) -> tuple[list[float], list[float]]:
    since = now - timedelta(days=days + 1)
    btc = _returns(session, cfg.symbol("BTC"), since)
    mine = _returns(session, cfg.symbol(asset), since)
    ts = sorted(set(mine) & set(btc))
    return [mine[x] for x in ts], [btc[x] for x in ts]


def betas(session: Session, cfg: Config, now: datetime) -> dict[str, Decimal]:
    """30-day beta to BTC per traded asset on 4-hour returns (owner 2026-10-10)."""
    out: dict[str, Decimal] = {}
    for asset in cfg.trading.assets:
        if asset == "BTC":
            out[asset] = ONE
            continue
        a, b = _aligned(session, cfg, asset, BETA_DAYS, now)
        out[asset] = beta_from_returns(a, b)
    return out


@dataclass(frozen=True)
class BetaRow:
    asset: str
    beta_30d: Decimal
    beta_90d: Decimal
    downside_beta_90d: Decimal  # beta on BTC's down 4h bars only
    corr_90d: float | None
    samples_90d: int


def correlation(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < MIN_POINTS:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    return round(float(cov / (va * vb) ** 0.5), 3)


def beta_table(session: Session, cfg: Config, now: datetime) -> list[BetaRow]:
    """For the admin: 4h-return betas over 30 and 90 days, the downside beta (BTC's down
    bars only) and the 90-day correlation, for every traded asset."""
    rows = []
    for asset in cfg.trading.assets:
        a30, b30 = _aligned(session, cfg, asset, 30, now)
        a90, b90 = _aligned(session, cfg, asset, 90, now)
        down = [(x, y) for x, y in zip(a90, b90, strict=True) if y < 0]
        rows.append(
            BetaRow(
                asset,
                ONE if asset == "BTC" else beta_from_returns(a30, b30),
                ONE if asset == "BTC" else beta_from_returns(a90, b90),
                ONE
                if asset == "BTC"
                else beta_from_returns([x for x, _ in down], [y for _, y in down]),
                1.0 if asset == "BTC" else correlation(a90, b90),
                len(a90),
            )
        )
    return rows


@dataclass(frozen=True)
class Exposure:
    track: str
    net_beta: Decimal  # signed, in quote: + net long crypto, − net short
    gross: Decimal  # sum of |notional|, in quote


def net_beta_exposure(
    positions: list[tuple[str, Decimal, str]], beta: dict[str, Decimal]
) -> Decimal:
    """Σ sign × notional × beta over (direction, notional, asset)."""
    total = Decimal(0)
    for direction, notional, asset in positions:
        sign = ONE if direction == "long" else -ONE
        total += sign * notional * beta.get(asset, ONE)
    return total


def track_exposure(session: Session, track: str, beta: dict[str, Decimal]) -> Exposure:
    rows = session.execute(
        select(Position.direction, Position.notional, Position.asset).where(
            Position.track == track, Position.status.in_(["open", "closing"])
        )
    ).all()
    triples = [(r.direction, Decimal(str(r.notional)), r.asset) for r in rows]
    return Exposure(
        track, net_beta_exposure(triples, beta), sum((n for _, n, _ in triples), Decimal(0))
    )
