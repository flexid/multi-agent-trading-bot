"""Triggered cycles (SPEC §7.5; limits in config [triggers] since v10).

- price: a move of more than `price_atr_1h` ATR(14, 4h) within the last hour, on the
  executor's latest quote (checked every 5 minutes)
- polymarket: a mapped market's P(yes) shifted more than 10 pp in the last hour
- x_shock: a credible fresh news-shock post in the last hour (labelled by Jev every few
  minutes from the curated accounts)
- formula: the hourly light cycle's code score flipped on an asset (app/decision/cycle.py)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Config
from app.db.models import (
    Candle,
    Cycle,
    Mark,
    PolymarketMarket,
    PolymarketPrice,
    SuppressedTrigger,
    XPostRecord,
)

POLY_SHIFT = Decimal("0.10")
MARK_MAX_AGE = timedelta(minutes=2)


def triggered_today(session: Session, now: datetime) -> int:
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        session.execute(
            select(func.count())
            .select_from(Cycle)
            .where(Cycle.kind.in_(["triggered", "goal"]), Cycle.started_at >= day)
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
        if len(recent) < 2:
            continue
        last = recent[-1]
        mark = session.get(Mark, symbol)  # the executor's quote: minutes fresher than a candle
        if mark is not None and now - mark.ts <= MARK_MAX_AGE:
            last = (mark.bid + mark.ask) / 2
        mult = cfg.triggers.price_atr_1h
        if abs(last - recent[0]) > mult * atr:
            move = float((last / recent[0] - 1) * 100)
            return f"price: {asset} moved {move:+.1f}% in 1h (> {mult}×ATR)"
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
            XPostRecord.fresh.is_not(False),  # a recap of an old shock is not news
            XPostRecord.credibility >= Decimal("0.6"),
            XPostRecord.created_at >= now - timedelta(hours=1),
        )
        .limit(1)
    ).first()
    return f"x_shock: credible news shock on {row.asset or 'market'}" if row else None


ASSET_IN_REASON = re.compile(r"\b(BTC|ETH|SOL|XRP|DOGE|PEPE|HBAR|PUMP|SPX6900|ENA|BNB)\b")
SUPPRESS_DEDUPE = timedelta(hours=1)  # the same condition persists; log it once an hour


def asset_of(reason: str) -> str | None:
    m = ASSET_IN_REASON.search(reason)
    return m.group(1) if m else None


def _spot(session: Session, cfg: Config, asset: str, at: datetime) -> Decimal | None:
    row = session.execute(
        select(Candle.close)
        .where(
            Candle.symbol == cfg.symbol(asset),
            Candle.interval == "15",
            Candle.open_time <= at,
        )
        .order_by(Candle.open_time.desc())
        .limit(1)
    ).scalar_one_or_none()
    return Decimal(str(row)) if row is not None else None


def log_suppressed(session: Session, cfg: Config, now: datetime, reason: str) -> bool:
    """Record a trigger the daily cap blocked (owner 2026-10-10). Logging only."""
    recent = session.execute(
        select(SuppressedTrigger.id).where(
            SuppressedTrigger.reason == reason, SuppressedTrigger.ts >= now - SUPPRESS_DEDUPE
        )
    ).first()
    if recent:
        return False
    asset = asset_of(reason) or "BTC"  # a market-wide shock is measured on BTC
    session.add(
        SuppressedTrigger(
            ts=now, reason=reason, asset=asset_of(reason), spot=_spot(session, cfg, asset, now)
        )
    )
    session.commit()
    return True


def fill_moves(session: Session, cfg: Config, now: datetime | None = None) -> int:
    """Fill the 4-hour and 1-day price moves of logged suppressed triggers once that much
    time has passed. Returns rows touched."""
    now = now or datetime.now(UTC)
    rows = session.scalars(
        select(SuppressedTrigger).where(
            SuppressedTrigger.spot.is_not(None), SuppressedTrigger.move_1d_pct.is_(None)
        )
    ).all()
    touched = 0
    for r in rows:
        asset = r.asset or "BTC"
        if r.move_4h_pct is None and now >= r.ts + timedelta(hours=4):
            later = _spot(session, cfg, asset, r.ts + timedelta(hours=4))
            if later is not None and r.spot:
                r.move_4h_pct = round(float((later / r.spot - 1) * 100), 3)
                touched += 1
        if now >= r.ts + timedelta(days=1):
            later = _spot(session, cfg, asset, r.ts + timedelta(days=1))
            if later is not None and r.spot:
                r.move_1d_pct = round(float((later / r.spot - 1) * 100), 3)
                touched += 1
    session.commit()
    return touched


def check(
    session: Session, cfg: Config, now: datetime | None = None, *, reason: str | None = None
) -> str | None:
    """The reason to trigger a cycle now, or None. ``reason``: a trigger found elsewhere
    (the light cycle's formula flip), put through the same gap and cap."""
    now = now or datetime.now(UTC)
    last = session.execute(
        select(func.max(Cycle.started_at)).where(Cycle.kind != "light")
    ).scalar_one()
    if last and now - last < timedelta(minutes=cfg.triggers.min_gap_minutes):
        return None  # a cycle just ran; its decisions stand
    # v11: no Polymarket trigger; its daily markets fired most of the v10 week's 32
    # triggered cycles ($26) for a reading that weighs almost nothing.
    reason = reason or (price_trigger(session, cfg, now) or x_shock_trigger(session, now))
    if reason and reason.startswith("x_shock") and cfg.triggers.same_story_hours > 0:
        # v14: one triggered cycle per story. The same post stays fresh for an hour and
        # fired three cycles 30 minutes apart on 2026-10-24.
        again = session.execute(
            select(Cycle.id).where(
                Cycle.trigger == reason[:80],
                Cycle.started_at >= now - timedelta(hours=cfg.triggers.same_story_hours),
            )
        ).first()
        if again is not None:
            return None
    if reason and triggered_today(session, now) >= cfg.triggers.max_per_day:
        log_suppressed(session, cfg, now, reason)  # the cap stands; only the log changes
        return None
    return reason
