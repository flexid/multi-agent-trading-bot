"""Live order gateway on Bybit spot margin (SPEC §9), used in pilot and live modes.

Entry: post-only limit at the touch on our side (bid for buys, ask for sells); if not
filled after ``REPRICE_S`` it is cancelled and re-placed once at the new touch; still not
filled after that → cancelled, outcome PARTIAL (with the filled part) or REJECTED. Shorts
borrow the base coin first (``isLeverage=1`` lets Bybit auto-borrow on the sell); exits
repay automatically on the buy-back. Every order carries our ``orderLinkId``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from decimal import Decimal

from app.execution.bybit_client import BybitAPIError, BybitClient, BybitError
from app.execution.bybit_models import OrderRequest
from app.execution.bybit_models import Side as ExSide
from app.execution.gateway import OrderOutcome, Outcome
from app.execution.simulator import Quote, Side

log = logging.getLogger("bybit_gateway")
REPRICE_S = 120
POLL_S = 3
BORROW_ERRORS = {170033, 170034, 170035, 170041, 170131, 170132}  # insufficient borrow / margin


class BybitGateway:
    name = "bybit"

    def __init__(self, client: BybitClient) -> None:
        self.client = client

    async def _work(
        self, symbol: str, ex_side: ExSide, qty: Decimal, price: Decimal, link_id: str, borrow: bool
    ) -> OrderOutcome:
        inst = await self.client.instrument(symbol)
        if inst is None:
            return OrderOutcome(Outcome.REJECTED, detail="unknown symbol")
        qty = inst.round_qty(qty)
        price = inst.round_price(price, up=ex_side is ExSide.SELL)
        filled_total, fee_total, value_total = Decimal(0), Decimal(0), Decimal(0)
        for attempt, lid in ((1, link_id), (2, f"{link_id}-r")):
            try:
                await self.client.place_order(
                    OrderRequest(
                        symbol=symbol,
                        side=ex_side,
                        qty=qty - filled_total,
                        price=price,
                        order_link_id=lid,
                        post_only=True,
                        is_leverage=borrow,
                    )
                )
            except BybitAPIError as exc:
                if exc.ret_code in BORROW_ERRORS:
                    return OrderOutcome(
                        Outcome.BORROW_FAILED, filled_total, None, fee_total, exc.ret_msg
                    )
                if attempt == 1:
                    return OrderOutcome(Outcome.REJECTED, detail=f"{exc.ret_code}: {exc.ret_msg}")
                break
            except BybitError as exc:
                return OrderOutcome(
                    Outcome.UNREACHABLE, filled_total, None, fee_total, str(exc)[:200]
                )
            filled, avg, fee = await self._wait_fill(symbol, lid)
            filled_total += filled
            value_total += filled * (avg or price)
            fee_total += fee
            if filled_total >= qty:
                break
            with contextlib.suppress(BybitError):
                await self.client.cancel_order(symbol, lid)
            if attempt == 1:
                t = await self.client.ticker(symbol)
                price = inst.round_price(
                    t.bid1_price if ex_side is ExSide.BUY else t.ask1_price,
                    up=ex_side is ExSide.SELL,
                )
        if filled_total <= 0:
            return OrderOutcome(Outcome.REJECTED, detail="not filled within two post-only attempts")
        avg_price = value_total / filled_total
        outcome = Outcome.FILLED if filled_total >= qty else Outcome.PARTIAL
        return OrderOutcome(outcome, filled_total, avg_price, fee_total)

    async def _wait_fill(
        self, symbol: str, link_id: str
    ) -> tuple[Decimal, Decimal | None, Decimal]:
        """Poll the order until filled, cancelled or REPRICE_S elapsed."""
        waited = 0
        while waited < REPRICE_S:
            await asyncio.sleep(POLL_S)
            waited += POLL_S
            try:
                orders = await self.client.open_orders(symbol)
            except BybitError:
                continue
            mine = next((o for o in orders if o.order_link_id == link_id), None)
            if mine is None:  # gone from the open book: filled or cancelled
                fill = await self._history(symbol, link_id)
                return fill
            if mine.cum_exec_qty >= mine.qty:
                return await self._history(symbol, link_id)
        return await self._history(symbol, link_id)

    async def _history(self, symbol: str, link_id: str) -> tuple[Decimal, Decimal | None, Decimal]:
        try:
            result = await self.client._get(  # noqa: SLF001 (gateway is the client's peer)
                "/v5/order/history",
                {"category": "spot", "symbol": symbol, "orderLinkId": link_id},
                auth=True,
            )
        except BybitError:
            return Decimal(0), None, Decimal(0)
        rows = result.get("list") or []
        if not rows:
            return Decimal(0), None, Decimal(0)
        row = rows[0]
        filled = Decimal(row.get("cumExecQty") or 0)
        avg = Decimal(row["avgPrice"]) if row.get("avgPrice") else None
        fee = Decimal(row.get("cumExecFee") or 0)
        return filled, avg, fee

    async def open(
        self, symbol: str, side: Side, qty: Decimal, borrow: bool, link_id: str, quote: Quote
    ) -> OrderOutcome:
        ex_side = ExSide.BUY if side is Side.LONG else ExSide.SELL
        price = quote.bid if side is Side.LONG else quote.ask
        return await self._work(symbol, ex_side, qty, price, link_id, borrow or side is Side.SHORT)

    async def close(
        self, symbol: str, side: Side, qty: Decimal, link_id: str, quote: Quote
    ) -> OrderOutcome:
        ex_side = ExSide.SELL if side is Side.LONG else ExSide.BUY
        price = quote.ask if side is Side.LONG else quote.bid
        return await self._work(symbol, ex_side, qty, price, link_id, True)
