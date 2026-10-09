"""Triggered cycles (SPEC §7.5): at most 2 per day, checked after every 15-minute fetch.

- price: a move of more than 2× ATR(14, 4h) within the last hour
- polymarket: a mapped market's P(yes) shifted more than 10 pp in the last hour
- x_shock: a credible news-shock post in the last hour (labelled by the X agent)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Config
from app.db.models import Candle, Cycle, PolymarketMarket, PolymarketPrice, XPostRecord

MAX_TRIGGERED_PER_DAY = 2
ATR_MULT = Decimal(2)
POLY_SHIFT = Decimal("0.10")


def triggered_today(session: Session, now: datetime) -> int:
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        session.execute(
            select(func.count())
            .select_from(Cycle)
            .where(Cycle.kind == "triggered", Cycle.started_at >= day)
        ).scalar_one()
    )


def price_trigger(session: Session, cfg: Config, now: datetime) -> str | None:
    for asset in cfg.trading.assets:
        symbol = cfg.symbol(asset)
        rows = session.execute(
            select(Candle.high, Candle.low, Candle.close)
            .where(Candle.symbol == symbol, Candle.interval == "240")
            .order_by(Candle.open_time.desc())
            .limit(15)
        ).all()
        if len(rows) < 15:
            continue
        trs = [
            max(h - lo, abs(h - pc), abs(lo - pc))
            for (h, lo, _), (_, _, pc) in zip(rows[:-1], rows[1:], strict=False)
        ]
        atr = sum(trs, Decimal(0)) / len(trs)
        recent = (
            session.execute(
                select(Candle.close)
                .where(
                    Candle.symbol == symbol,
                    Candle.interval == "15",
                    Candle.open_time >= now - timedelta(hours=1, minutes=15),
                )
                .order_by(Candle.open_time)
            )
            .scalars()
            .all()
        )
        if len(recent) >= 2 and abs(recent[-1] - recent[0]) > ATR_MULT * atr:
            move = float((recent[-1] / recent[0] - 1) * 100)
            return f"price: {asset} moved {move:+.1f}% in 1h (> 2×ATR)"
    return None


POLY_MIN_HOURS_LEFT = 6  # a daily market swings to 0 or 100 by itself near resolution
POLY_MIN_VOLUME = Decimal(1000)


def polymarket_trigger(session: Session, now: datetime) -> str | None:
    """A > 10 pp move within an hour on a threshold market the agent would use: mapped as
    a price market, liquid, and not in its last hours before resolution."""
    markets = session.scalars(
        select(PolymarketMarket).where(
            PolymarketMarket.asset.is_not(None),
            PolymarketMarket.closed.is_(False),
            PolymarketMarket.end_date > now + timedelta(hours=POLY_MIN_HOURS_LEFT),
        )
    ).all()
    for m in markets:
        if (m.mapping or {}).get("kind") != "price":
            continue
        prices = session.execute(
            select(PolymarketPrice.ts, PolymarketPrice.prices, PolymarketPrice.volume_24h)
            .where(
                PolymarketPrice.market_id == m.id,
                PolymarketPrice.ts >= now - timedelta(hours=1, minutes=5),
            )
            .order_by(PolymarketPrice.ts)
        ).all()
        if len(prices) < 2 or not prices[0].prices:
            continue
        if Decimal(str(prices[-1].volume_24h or 0)) < POLY_MIN_VOLUME:
            continue
        shift = Decimal(str(prices[-1].prices[0])) - Decimal(str(prices[0].prices[0]))
        if abs(shift) > POLY_SHIFT:
            return f"polymarket: {m.asset} market {m.id} shifted {float(shift) * 100:+.0f} pp in 1h"
    return None


def x_shock_trigger(session: Session, now: datetime) -> str | None:
    row = session.execute(
        select(XPostRecord.asset)
        .where(
            XPostRecord.shock.is_(True),
            XPostRecord.credibility >= Decimal("0.6"),
            XPostRecord.created_at >= now - timedelta(hours=1),
        )
        .limit(1)
    ).first()
    return f"x_shock: credible news shock on {row.asset or 'market'}" if row else None


def check(session: Session, cfg: Config, now: datetime | None = None) -> str | None:
    """The reason to trigger a cycle now, or None."""
    now = now or datetime.now(UTC)
    if triggered_today(session, now) >= MAX_TRIGGERED_PER_DAY:
        return None
    last = session.execute(select(func.max(Cycle.started_at))).scalar_one()
    if last and now - last < timedelta(hours=1):
        return None  # a cycle just ran; its decisions stand
    return (
        price_trigger(session, cfg, now)
        or polymarket_trigger(session, now)
        or x_shock_trigger(session, now)
    )
