"""Paper fills against a real order book.

M0 scope: a marketable order fills in full at the touch (buy at ask, sell at bid)
and pays the taker fee. Borrow interest, leverage and liquidation arrive in M6.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.execution.bybit_models import OrderBook, Side


class PaperFill(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    fee_quote: Decimal

    @property
    def notional(self) -> Decimal:
        return self.qty * self.price


def paper_fill(book: OrderBook, side: Side, qty: Decimal, taker_fee_rate: Decimal) -> PaperFill:
    if qty <= 0:
        raise ValueError("qty must be positive")
    price = book.best_ask.price if side is Side.BUY else book.best_bid.price
    return PaperFill(
        symbol=book.symbol,
        side=side,
        qty=qty,
        price=price,
        fee_quote=qty * price * taker_fee_rate,
    )


def round_trip_pnl(entry: PaperFill, exit_: PaperFill) -> Decimal:
    """Net quote-coin P&L of an entry and its opposite exit, after fees."""
    if entry.side is exit_.side or entry.qty != exit_.qty:
        raise ValueError("exit must be the opposite side for the same quantity")
    direction = Decimal(1) if entry.side is Side.BUY else Decimal(-1)
    gross = direction * (exit_.price - entry.price) * entry.qty
    return gross - entry.fee_quote - exit_.fee_quote
