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
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config
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
    day_pnl_by_direction: dict[str, Decimal] | None = None  # today, quote, long/short


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
    s: Any = cfg.sleeves[name]
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
    by_dir: dict[str, Decimal] = {}
    for p in rows:
        today = Decimal(0)
        if p.status == "closed" and p.closed_at and p.closed_at >= day0:
            today = p.pnl or Decimal(0)
        elif p.status in ("open", "closing"):
            today = unrealized(session, p, now)
        by_dir[p.direction] = by_dir.get(p.direction, Decimal(0)) + today
    triples = [(p.direction, p.notional, p.asset) for p in open_rows]
    return SleeveAccount(
        name=name,
        capital=capital,
        equity=equity,
        day_pnl_pct=day_pnl,
        gross_exposure=sum((p.notional for p in open_rows), Decimal(0)),
        net_beta_exposure=net_beta_exposure(triples, beta),
        open_positions=len(open_rows),
        day_pnl_by_direction=by_dir,
    )


DIRECTIONS = ("long", "short")


def lock_info(locks: dict[str, Any] | None, name: str, now: datetime) -> dict[str, Any] | None:
    """The sleeve's active day lock, or None. ``risk_state.day_locked_sleeves`` holds
    ``{sleeve: {"until", "dirs", "since", "pnl_at_lock"}}``; the first format, a bare
    ``until`` string, means both directions with no start time."""
    raw = (locks or {}).get(name)
    if not raw:
        return None
    if isinstance(raw, str):
        raw = {"until": raw}
    until = datetime.fromisoformat(str(raw["until"]))
    if until <= now:
        return None
    since = raw.get("since")
    pnl = raw.get("pnl_at_lock")
    return {
        "until": until,
        "dirs": set(raw.get("dirs") or DIRECTIONS),
        "since": datetime.fromisoformat(str(since)) if since else None,
        "pnl_at_lock": Decimal(str(pnl)) if pnl is not None else None,
    }


def day_locked(
    locks: dict[str, Any] | None, name: str, now: datetime, direction: str | None = None
) -> bool:
    """Is the sleeve locked today, for ``direction`` when one is given? Since logic v8 a
    day-loss stop closes the sleeve but only locks new trades in the direction that lost."""
    info = lock_info(locks, name, now)
    if info is None:
        return False
    return direction is None or direction in info["dirs"]


def lock_until_midnight(now: datetime) -> str:
    return (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).isoformat()


def new_lock(now: datetime, dirs: set[str] | list[str], day_pnl_pct: Decimal) -> dict[str, Any]:
    return {
        "until": lock_until_midnight(now),
        "dirs": sorted(dirs),
        "since": now.isoformat(),
        "pnl_at_lock": str(day_pnl_pct),
    }


def losing_directions(by_direction: dict[str, Decimal]) -> set[str]:
    """The directions that lost money today; both when that cannot be told."""
    lost = {d for d in DIRECTIONS if by_direction.get(d, Decimal(0)) < 0}
    return lost or set(DIRECTIONS)


def directions_to_lock(
    info: dict[str, Any] | None,
    day_pnl_pct: Decimal,
    day_loss_stop: Decimal,
    by_direction: dict[str, Decimal] | None,
) -> set[str] | None:
    """Which directions the day-loss stop locks now, or None when it does not fire.

    No lock yet: it fires at the stop and locks the direction(s) that lost. Already locked
    one way: the loss that set the lock must not fire it again, so it fires only when the
    sleeve has lost the stop's size once more since then, and then locks everything."""
    if info is None:
        return losing_directions(by_direction or {}) if day_pnl_pct <= day_loss_stop else None
    if len(info["dirs"]) >= 2 or info["pnl_at_lock"] is None:
        return None
    return set(DIRECTIONS) if day_pnl_pct <= info["pnl_at_lock"] + day_loss_stop else None
