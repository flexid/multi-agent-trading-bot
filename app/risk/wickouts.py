"""Wick-out bookkeeping (owner 2026-10-10): after a stopped-out trade's planned holding
time has passed, look at the 15-minute candles between the stop-out and the end of that
window; if price reached the original target, the stop was a wick-out. The rate per asset
shows on the admin and moves the stop buffer at tuning time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Candle, Position, RiskState
from app.risk.stops import next_buffer, wick_out_for

STOP_REASONS = ("stop", "stop_hard")


def check(session: Session, now: datetime | None = None) -> int:
    """Fill ``wick_out`` for stopped trades whose holding window has ended."""
    now = now or datetime.now(UTC)
    rows = session.scalars(
        select(Position).where(
            Position.status == "closed",
            Position.close_reason.in_(STOP_REASONS),
            Position.wick_out.is_(None),
        )
    ).all()
    done = 0
    for p in rows:
        if not p.opened_at or not p.closed_at:
            continue
        window_end = p.opened_at + timedelta(hours=p.max_hold_hours)
        if now < window_end:
            continue
        candles = session.execute(
            select(Candle.high, Candle.low).where(
                Candle.symbol == p.symbol,
                Candle.interval == "15",
                Candle.open_time >= p.closed_at,
                Candle.open_time < window_end,
            )
        ).all()
        highs = [Decimal(str(c.high)) for c in candles]
        lows = [Decimal(str(c.low)) for c in candles]
        p.wick_out = wick_out_for(p.direction, p.target, highs, lows)
        done += 1
    session.commit()
    return done


@dataclass(frozen=True)
class WickStats:
    asset: str
    stopped: int
    wick_outs: int

    @property
    def rate(self) -> float | None:
        return self.wick_outs / self.stopped if self.stopped else None


def stats(session: Session, assets: list[str], track: str = "primary") -> list[WickStats]:
    rows = session.scalars(
        select(Position).where(
            Position.track == track,
            Position.status == "closed",
            Position.close_reason.in_(STOP_REASONS),
            Position.wick_out.is_not(None),
        )
    ).all()
    out = []
    for a in assets:
        mine = [p for p in rows if p.asset == a]
        out.append(WickStats(a, len(mine), sum(1 for p in mine if p.wick_out)))
    return out


def tune_buffer(session: Session, assets: list[str]) -> Decimal:
    """Move ``risk_state.stop_buffer_atr`` one step on the primary track's wick-out rate."""
    state = session.get(RiskState, 1)
    if state is None:
        return Decimal("0.5")
    current = state.stop_buffer_atr or Decimal("0.5")
    allst = stats(session, assets)
    stopped = sum(s.stopped for s in allst)
    wicks = sum(s.wick_outs for s in allst)
    new = next_buffer(current, wicks / stopped if stopped else None, stopped)
    if new != current:
        state.stop_buffer_atr = new
        session.commit()
    return new
