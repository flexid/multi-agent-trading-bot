"""Two sleeves of one book (owner 2026-10-10): each has its own capital share, risk
budget, caps and day-loss stop, and its own go-live evaluation. The ledgers stay per
track; a sleeve's view is computed from its positions.

Sleeve equity  = sleeve share of the track's starting capital + realized P&L of its
                 closed positions + unrealized P&L of its open ones (last 15m close)
Day P&L        = realized today + unrealized of the open positions, over the sleeve's
                 start-of-day capital (an approximation that errs on the cautious side)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config, SleeveConfig
from app.db.models import Candle, PaperAccount, Position
from app.risk.exposure import net_beta_exposure


@dataclass(frozen=True)
class SleeveAccount:
    name: str
    capital: Decimal  # share of the track's starting capital
    equity: Decimal
    day_pnl_pct: Decimal
    gross_exposure: Decimal
    net_beta_exposure: Decimal
    open_positions: int


def _last_close(session: Session, symbol: str, now: datetime) -> Decimal | None:
    row = session.execute(
        select(Candle.close)
        .where(Candle.symbol == symbol, Candle.interval == "15", Candle.open_time <= now)
        .order_by(Candle.open_time.desc())
        .limit(1)
    ).scalar_one_or_none()
    return Decimal(str(row)) if row is not None else None


def unrealized(session: Session, p: Position, now: datetime) -> Decimal:
    price = _last_close(session, p.symbol, now)
    if price is None or not p.entry_price:
        return Decimal(0)
    sign = Decimal(1) if p.direction == "long" else Decimal(-1)
    return sign * (price - p.entry_price) * p.qty - p.fees - p.interest


def sleeve_account(
    session: Session,
    cfg: Config,
    name: str,
    track: str,
    now: datetime,
    beta: dict[str, Decimal],
    ledger_id: int,
) -> SleeveAccount:
    s: SleeveConfig = cfg.sleeves[name]
    acct = session.get(PaperAccount, ledger_id)
    start = (
        acct.starting_capital if acct and acct.starting_capital else cfg.trading.capital_max_usdt
    )
    capital = start * s.capital_fraction
    rows = session.scalars(
        select(Position).where(Position.track == track, Position.asset.in_(s.assets))
    ).all()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    realized = sum((p.pnl or Decimal(0) for p in rows if p.status == "closed"), Decimal(0))
    realized_today = sum(
        (
            p.pnl or Decimal(0)
            for p in rows
            if p.status == "closed" and p.closed_at and p.closed_at >= day0
        ),
        Decimal(0),
    )
    open_rows = [p for p in rows if p.status in ("open", "closing")]
    unreal = sum((unrealized(session, p, now) for p in open_rows), Decimal(0))
    equity = capital + realized + unreal
    day_pnl = (realized_today + unreal) / capital if capital else Decimal(0)
    triples = [(p.direction, p.notional, p.asset) for p in open_rows]
    return SleeveAccount(
        name=name,
        capital=capital,
        equity=equity,
        day_pnl_pct=day_pnl,
        gross_exposure=sum((p.notional for p in open_rows), Decimal(0)),
        net_beta_exposure=net_beta_exposure(triples, beta),
        open_positions=len(open_rows),
    )


def day_locked(state_locks: dict[str, str] | None, name: str, now: datetime) -> bool:
    """``risk_state.day_locked_sleeves`` holds {sleeve: iso-until}."""
    if not state_locks or name not in state_locks:
        return False
    return datetime.fromisoformat(state_locks[name]) > now


def lock_until_midnight(now: datetime) -> str:
    return (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).isoformat()
