"""Order gateway: the one seam between the executor and the venue.

``PaperGateway`` fills against the current quote (shadow mode). ``BybitGateway`` places
real post-only limit orders with an ``orderLinkId`` and reports what the exchange says.
Tests drive the executor with a scripted gateway to reproduce the failure modes the owner
listed: partial fill, rejected order, failed borrow, exchange unreachable.

Every call returns an ``OrderOutcome``; the gateway never raises for venue-side
problems. ``UNREACHABLE`` means "nothing happened, retry later"; the executor keeps its
stops armed and retries on the next tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.execution.simulator import Quote, Side


class Outcome(StrEnum):
    FILLED = "filled"
    PARTIAL = "partial"  # some quantity filled, the rest cancelled
    REJECTED = "rejected"  # the venue refused (e.g. insufficient balance, bad price)
    BORROW_FAILED = "borrow_failed"  # margin borrow unavailable
    UNREACHABLE = "unreachable"  # network / venue down: nothing happened


@dataclass(frozen=True)
class OrderOutcome:
    outcome: Outcome
    filled_qty: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    fee: Decimal = Decimal(0)
    detail: str = ""


class Gateway(Protocol):
    name: str

    async def open(
        self, symbol: str, side: Side, qty: Decimal, borrow: bool, link_id: str, quote: Quote
    ) -> OrderOutcome: ...

    async def close(
        self, symbol: str, side: Side, qty: Decimal, link_id: str, quote: Quote
    ) -> OrderOutcome: ...


class PaperGateway:
    """Fills at the touch, full quantity, taker fee. Shadow mode."""

    name = "paper"

    def __init__(self, taker_fee: Decimal) -> None:
        self.fee = taker_fee

    async def open(
        self, symbol: str, side: Side, qty: Decimal, borrow: bool, link_id: str, quote: Quote
    ) -> OrderOutcome:
        price = quote.ask if side is Side.LONG else quote.bid
        return OrderOutcome(Outcome.FILLED, qty, price, qty * price * self.fee)

    async def close(
        self, symbol: str, side: Side, qty: Decimal, link_id: str, quote: Quote
    ) -> OrderOutcome:
        price = quote.bid if side is Side.LONG else quote.ask
        return OrderOutcome(Outcome.FILLED, qty, price, qty * price * self.fee)


class ScriptedGateway:
    """Test double: returns the next scripted outcome for each call (last one repeats)."""

    name = "scripted"

    def __init__(self, opens: list[OrderOutcome], closes: list[OrderOutcome] | None = None) -> None:
        self.opens = list(opens)
        self.closes = list(closes or [OrderOutcome(Outcome.FILLED)])
        self.calls: list[tuple[str, str, Side, Decimal]] = []

    def _next(
        self, queue: list[OrderOutcome], qty: Decimal, quote: Quote, side: Side, opening: bool
    ) -> OrderOutcome:
        out = queue.pop(0) if len(queue) > 1 else queue[0]
        price = out.avg_price
        if price is None and out.outcome in (Outcome.FILLED, Outcome.PARTIAL):
            price = (
                (quote.ask if side is Side.LONG else quote.bid)
                if opening
                else (quote.bid if side is Side.LONG else quote.ask)
            )
        filled = (
            out.filled_qty
            if out.outcome is Outcome.PARTIAL
            else (qty if out.outcome is Outcome.FILLED else Decimal(0))
        )
        return OrderOutcome(out.outcome, filled, price, out.fee, out.detail)

    async def open(
        self, symbol: str, side: Side, qty: Decimal, borrow: bool, link_id: str, quote: Quote
    ) -> OrderOutcome:
        self.calls.append(("open", symbol, side, qty))
        return self._next(self.opens, qty, quote, side, True)

    async def close(
        self, symbol: str, side: Side, qty: Decimal, link_id: str, quote: Quote
    ) -> OrderOutcome:
        self.calls.append(("close", symbol, side, qty))
        return self._next(self.closes, qty, quote, side, False)
