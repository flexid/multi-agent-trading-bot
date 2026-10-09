"""Paper-fill simulator for shadow mode (SPEC §9): pure functions on a position ledger.

- fills at the touch: buy at ask, sell at bid, plus the taker fee
- borrow interest accrues hourly on the borrowed part (quote for leveraged longs, the
  whole base quantity for shorts) at the exchange's hourly rate
- liquidation price from Bybit's cross-margin rule for an isolated paper position: the
  coin side is haircut by its collateral ratio (API), the liability carries the
  maintenance margin rate; the paper account does not pool collateral across positions
- exits: stop, target, trailing stop (armed after +1R, trails at 1R), time-stop, kill
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

MAINTENANCE = Decimal("0.03")  # default; the executor passes the configured rate
TRAIL_ARM_R = Decimal(1)  # arm the trailing stop after one stop-distance of profit
TRAIL_R = Decimal(1)  # then trail one stop-distance behind the best price


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"


class ExitReason(StrEnum):
    STOP = "stop"
    TARGET = "target"
    TRAIL = "trail"
    TIME = "time"
    KILL = "kill"
    LIQUIDATION = "liquidation"
    RISK = "risk"  # day loss stop, drawdown pause, close-all


@dataclass(frozen=True)
class Quote:
    bid: Decimal
    ask: Decimal
    ts: datetime

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class PaperPosition:
    side: Side
    qty: Decimal
    entry: Decimal
    leverage: Decimal
    margin: Decimal
    borrowed: Decimal  # quote (long) or base qty (short)
    stop: Decimal
    target: Decimal
    opened_at: datetime
    max_hold_hours: int
    fees: Decimal = Decimal(0)
    interest: Decimal = Decimal(0)
    best_price: Decimal | None = None
    trail_stop: Decimal | None = None
    last_interest_at: datetime | None = None
    collateral_ratio: Decimal = Decimal("0.98")  # of the base coin, from the API
    maintenance_rate: Decimal = MAINTENANCE

    @property
    def stop_distance(self) -> Decimal:
        return abs(self.entry - self.stop)

    @property
    def liquidation_price(self) -> Decimal:
        """Bybit cross-margin rule on this position alone.

        Long: holds qty coins (haircut c), owes borrowed USDT:  c·qty·P ≤ B·(1 + mm)
        Short: holds margin + proceeds in USDT (ratio 1), owes qty coins:
               margin + proceeds ≤ qty·P·(1 + mm)
        """
        mm = self.maintenance_rate
        if self.side is Side.LONG:
            if self.borrowed <= 0:
                return Decimal(0)
            return self.borrowed * (1 + mm) / (self.collateral_ratio * self.qty)
        proceeds = self.qty * self.entry
        return (self.margin + proceeds) / (self.qty * (1 + mm))


def round_qty(qty: Decimal, step: Decimal) -> Decimal:
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def open_position(
    side: Side,
    notional: Decimal,
    leverage: Decimal,
    quote: Quote,
    stop: Decimal,
    target: Decimal,
    max_hold_hours: int,
    taker_fee: Decimal,
    qty_step: Decimal,
    collateral_ratio: Decimal = Decimal("0.98"),
    maintenance_rate: Decimal = MAINTENANCE,
) -> PaperPosition:
    price = quote.ask if side is Side.LONG else quote.bid
    qty = round_qty(notional / price, qty_step)
    if qty <= 0:
        raise ValueError("notional below one lot")
    actual = qty * price
    margin = actual / leverage
    borrowed = (actual - margin) if side is Side.LONG else qty
    fee = actual * taker_fee
    return PaperPosition(
        side=side,
        qty=qty,
        entry=price,
        leverage=leverage,
        margin=margin,
        borrowed=borrowed,
        stop=stop,
        target=target,
        opened_at=quote.ts,
        max_hold_hours=max_hold_hours,
        fees=fee,
        last_interest_at=quote.ts,
        collateral_ratio=collateral_ratio,
        maintenance_rate=maintenance_rate,
    )


def accrue_interest(
    pos: PaperPosition, now: datetime, hourly_rate: Decimal, price: Decimal
) -> PaperPosition:
    since = pos.last_interest_at or pos.opened_at
    hours = Decimal(int((now - since).total_seconds() // 3600))
    if hours <= 0:
        return pos
    principal = pos.borrowed if pos.side is Side.LONG else pos.borrowed * price
    interest = principal * hourly_rate * hours
    return replace(
        pos, interest=pos.interest + interest, last_interest_at=since + timedelta(hours=int(hours))
    )


def update_trail(pos: PaperPosition, quote: Quote) -> PaperPosition:
    """Arm after +1R, then trail 1R behind the best price; the stop only ever tightens."""
    price = quote.bid if pos.side is Side.LONG else quote.ask
    best = pos.best_price
    best = (
        price if best is None else (max(best, price) if pos.side is Side.LONG else min(best, price))
    )
    r = pos.stop_distance
    trail = pos.trail_stop
    if pos.side is Side.LONG and best - pos.entry >= TRAIL_ARM_R * r:
        candidate = best - TRAIL_R * r
        trail = candidate if trail is None else max(trail, candidate)
    elif pos.side is Side.SHORT and pos.entry - best >= TRAIL_ARM_R * r:
        candidate = best + TRAIL_R * r
        trail = candidate if trail is None else min(trail, candidate)
    return replace(pos, best_price=best, trail_stop=trail)


def exit_reason(
    pos: PaperPosition, quote: Quote, now: datetime, kill: bool = False
) -> ExitReason | None:
    if kill:
        return ExitReason.KILL
    price = quote.bid if pos.side is Side.LONG else quote.ask  # the price we could exit at
    liq = pos.liquidation_price
    if pos.side is Side.LONG:
        if liq > 0 and price <= liq:
            return ExitReason.LIQUIDATION
        if price <= pos.stop:
            return ExitReason.STOP
        if pos.trail_stop is not None and price <= pos.trail_stop:
            return ExitReason.TRAIL
        if price >= pos.target:
            return ExitReason.TARGET
    else:
        if price >= liq:
            return ExitReason.LIQUIDATION
        if price >= pos.stop:
            return ExitReason.STOP
        if pos.trail_stop is not None and price >= pos.trail_stop:
            return ExitReason.TRAIL
        if price <= pos.target:
            return ExitReason.TARGET
    if now - pos.opened_at >= timedelta(hours=pos.max_hold_hours):
        return ExitReason.TIME
    return None


@dataclass(frozen=True)
class Fill:
    price: Decimal
    fee: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    pnl_price_pct: Decimal
    pnl_margin_pct: Decimal


def close_position(
    pos: PaperPosition, quote: Quote, taker_fee: Decimal, reason: ExitReason
) -> Fill:
    price = quote.bid if pos.side is Side.LONG else quote.ask
    if reason is ExitReason.LIQUIDATION:
        price = pos.liquidation_price
    gross = (
        (price - pos.entry) * pos.qty if pos.side is Side.LONG else (pos.entry - price) * pos.qty
    )
    fee = price * pos.qty * taker_fee
    net = gross - fee - pos.fees - pos.interest
    if reason is ExitReason.LIQUIDATION:
        net = -pos.margin  # the margin is gone
    price_pct = (price / pos.entry - 1) * (1 if pos.side is Side.LONG else -1)
    return Fill(
        price=price,
        fee=fee,
        gross_pnl=gross,
        net_pnl=net,
        pnl_price_pct=price_pct,
        pnl_margin_pct=net / pos.margin if pos.margin else Decimal(0),
    )


def unrealized(pos: PaperPosition, quote: Quote) -> Decimal:
    price = quote.bid if pos.side is Side.LONG else quote.ask
    gross = (
        (price - pos.entry) * pos.qty if pos.side is Side.LONG else (pos.entry - price) * pos.qty
    )
    return gross - pos.fees - pos.interest
