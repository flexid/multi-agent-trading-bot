"""Live order gateway on Bybit spot margin (SPEC §9), used in pilot and live modes.

Entry: post-only limit at the touch on our side (bid for buys, ask for sells); if not
filled after ``REPRICE_S`` it is cancelled and re-placed once at the new touch; still not
filled after that → cancelled, outcome PARTIAL (with the filled part) or REJECTED. Shorts
borrow the base coin first (``isLeverage=1`` lets Bybit auto-borrow on the sell); exits
repay automatically on the buy-back. Every order carries our ``orderLinkId``.

Exit: never post-only. An IOC limit priced ``EXIT_SLIPPAGE`` beyond the current touch
(below the bid for a sell, above the ask for a buy), so it crosses the spread and takes
what is there without chasing an empty book further than that. Whatever is left is
re-sent at the new touch, up to ``EXIT_ATTEMPTS`` times per call; the executor calls
again every tick until the position is flat. An attempt whose result is unknown (the
request or the read-back failed) is settled before anything else is sent, so a retry
never sells the same quantity twice.

Backup stop: a conditional stop-market resting on the exchange, in case this process or
its connection dies. It is placed without leverage, so a stale trigger can only fail; it
can never borrow and open a position of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from decimal import Decimal

from app.execution.bybit_client import BybitAPIError, BybitClient, BybitError
from app.execution.bybit_models import Instrument, Order, OrderRequest, TimeInForce
from app.execution.bybit_models import Side as ExSide
from app.execution.gateway import OrderOutcome, Outcome
from app.execution.simulator import Quote, Side

log = logging.getLogger("bybit_gateway")
REPRICE_S = 120
POLL_S = 3
EXIT_SLIPPAGE = Decimal("0.01")  # exit limit: 1% beyond the touch
COVER_FEE_BUFFER = Decimal("0.0012")  # taker fee plus rounding, so a cover repays the borrow
EXIT_ATTEMPTS = 5  # per call; the executor calls again every tick until flat
SETTLE_POLL_S = 0.3
SETTLE_POLLS = 5
STOP_FILTER = "StopOrder"
BORROW_ERRORS = {170033, 170034, 170035, 170041, 170131, 170132}  # insufficient borrow / margin
ORDER_NOT_FOUND = {170213, 110001}  # cancel: the order no longer exists


class BybitGateway:
    name = "bybit"
    supports_backup_stop = True

    def __init__(self, client: BybitClient) -> None:
        self.client = client
        # Exit attempts whose outcome we could not read, per position prefix.
        self._unsettled: dict[str, list[str]] = {}

    async def ensure_collateral(self, coins: list[str]) -> list[str]:
        """Switch the traded coins on as collateral (idempotent). Bybit refuses margin
        orders in a coin that is not collateral (retCode 170037)."""
        try:
            result = await self.client._post(  # noqa: SLF001
                "/v5/account/set-collateral-switch-batch",
                {"request": [{"coin": c, "collateralSwitch": "ON"} for c in coins]},
            )
        except BybitError as exc:
            log.warning("collateral switch failed: %s", exc)
            return []
        return [str(row["coin"]) for row in result.get("list", [])]

    # --- entries: post-only -------------------------------------------------------

    async def _work(
        self, symbol: str, ex_side: ExSide, qty: Decimal, price: Decimal, link_id: str, borrow: bool
    ) -> OrderOutcome:
        try:
            inst = await self.client.instrument(symbol)
        except BybitError as exc:
            return OrderOutcome(Outcome.UNREACHABLE, detail=str(exc)[:200])
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
                # The request may have reached the exchange: take the order off the book
                # if it is there, and keep whatever it filled.
                with contextlib.suppress(BybitError):
                    await self.client.cancel_order(symbol, lid)
                filled, avg, fee = await self._history(symbol, lid)
                filled_total += filled
                value_total += filled * (avg or price)
                fee_total += fee
                if filled_total <= 0:
                    return OrderOutcome(Outcome.UNREACHABLE, detail=str(exc)[:200])
                break
            filled, avg, fee = await self._wait_fill(symbol, lid)
            filled_total += filled
            value_total += filled * (avg or price)
            fee_total += fee
            if filled_total >= qty:
                break
            with contextlib.suppress(BybitError):
                await self.client.cancel_order(symbol, lid)
            if attempt == 1:
                try:
                    t = await self.client.ticker(symbol)
                except BybitError:
                    break  # no fresh touch to reprice at: keep what filled
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
            order = await self.client.order_history(symbol, link_id)
        except BybitError:
            return Decimal(0), None, Decimal(0)
        return _fill_of(order)

    async def open(
        self, symbol: str, side: Side, qty: Decimal, borrow: bool, link_id: str, quote: Quote
    ) -> OrderOutcome:
        ex_side = ExSide.BUY if side is Side.LONG else ExSide.SELL
        price = quote.bid if side is Side.LONG else quote.ask
        return await self._work(symbol, ex_side, qty, price, link_id, borrow or side is Side.SHORT)

    # --- exits: IOC with a price cap, retried until flat --------------------------

    async def _settled(self, symbol: str, link_id: str, *, acked: bool) -> Order | None:
        """The final state of an exit order, or None while it is unknown.

        ``acked``: the exchange confirmed the order, so not finding it yet only means the
        read is behind. Without an ack (the request failed in transit, and a tick has
        passed since) an order the exchange has never heard of was never placed.
        """
        seen = False
        for _ in range(SETTLE_POLLS):
            try:
                order = await self.client.find_order(symbol, link_id)
            except BybitError:
                return None
            if order is not None:
                seen = True
                if order.is_final:
                    return order
            await asyncio.sleep(SETTLE_POLL_S)
        if seen or acked:
            return None
        return _never_placed(symbol, link_id)

    async def close(
        self, symbol: str, side: Side, qty: Decimal, link_id: str, quote: Quote
    ) -> OrderOutcome:
        ex_side = ExSide.SELL if side is Side.LONG else ExSide.BUY
        try:
            inst = await self.client.instrument(symbol)
        except BybitError as exc:
            return OrderOutcome(Outcome.UNREACHABLE, detail=str(exc)[:200])
        if inst is None:
            return OrderOutcome(Outcome.REJECTED, detail="unknown symbol")
        qty = inst.round_qty(qty)
        # Spot fees are taken from the coin received: a long holds slightly less than it
        # bought, so sell what is actually there; a short must buy back a little more so
        # the borrow repays in full (Bybit auto-repays from the buy).
        try:
            if ex_side is ExSide.SELL:
                wallet = await self.client.wallet_balance()
                held = wallet.of(inst.base_coin)
                if held is not None:
                    qty = min(qty, inst.round_qty(held.wallet_balance))
            else:
                qty = inst.round_qty(qty / (Decimal(1) - COVER_FEE_BUFFER), up=True)
        except BybitError as exc:
            return OrderOutcome(Outcome.UNREACHABLE, detail=str(exc)[:200])
        filled_total, fee_total, value_total = Decimal(0), Decimal(0), Decimal(0)
        detail, blocked, rejected = "", False, False

        def book(order: Order) -> None:
            nonlocal filled_total, fee_total, value_total
            filled, avg, fee = _fill_of(order)
            filled_total += filled
            value_total += filled * (avg or Decimal(0))
            fee_total += fee

        # Settle earlier attempts first: sending more while one is unknown could double up.
        pending = self._unsettled.setdefault(link_id, [])
        for lid in list(pending):
            order = await self._settled(symbol, lid, acked=False)
            if order is None:
                blocked, detail = True, f"earlier exit {lid} still unknown"
                break
            pending.remove(lid)
            book(order)

        attempts = 0
        while not blocked and attempts < EXIT_ATTEMPTS:
            remaining = inst.round_qty(qty - filled_total)
            if remaining <= 0:
                break
            try:
                t = await self.client.ticker(symbol)
            except BybitError as exc:
                blocked, detail = True, str(exc)[:200]
                break
            touch = t.bid1_price if ex_side is ExSide.SELL else t.ask1_price
            if _is_dust(inst, remaining, touch):
                break  # below the exchange minimum: nothing left that can be traded
            limit = inst.round_price(
                touch * (1 - EXIT_SLIPPAGE if ex_side is ExSide.SELL else 1 + EXIT_SLIPPAGE),
                up=ex_side is ExSide.BUY,
            )
            lid = f"{link_id}{uuid.uuid4().hex[:6]}"
            attempts += 1
            try:
                await self.client.place_order(
                    OrderRequest(
                        symbol=symbol,
                        side=ex_side,
                        qty=remaining,
                        price=limit,
                        order_link_id=lid,
                        time_in_force=TimeInForce.IOC,
                        # Selling a held coin is plain spot; buying back a borrow repays it.
                        is_leverage=ex_side is ExSide.BUY,
                    )
                )
            except BybitAPIError as exc:
                rejected, detail = True, f"{exc.ret_code}: {exc.ret_msg}"
                break
            except BybitError as exc:
                # The request may or may not have reached the exchange.
                pending.append(lid)
                blocked, detail = True, str(exc)[:200]
                break
            order = await self._settled(symbol, lid, acked=True)
            if order is None:
                pending.append(lid)
                blocked, detail = True, f"exit {lid} sent, result unknown"
                break
            book(order)

        if not pending:
            self._unsettled.pop(link_id, None)
        avg_price = value_total / filled_total if filled_total > 0 else None
        left = inst.round_qty(qty - filled_total)
        if left <= 0 or (not blocked and not rejected and attempts < EXIT_ATTEMPTS):
            # Flat, or only untradeable dust is left.
            return OrderOutcome(Outcome.FILLED, filled_total, avg_price, fee_total, detail)
        if filled_total > 0:
            return OrderOutcome(Outcome.PARTIAL, filled_total, avg_price, fee_total, detail)
        if rejected:
            return OrderOutcome(Outcome.REJECTED, detail=detail)
        if blocked:
            return OrderOutcome(Outcome.UNREACHABLE, detail=detail)
        return OrderOutcome(
            Outcome.REJECTED, detail=f"nothing filled within {EXIT_SLIPPAGE:.0%} of the touch"
        )

    # --- exchange-side backup stop -------------------------------------------------

    async def place_backup_stop(
        self, symbol: str, side: Side, qty: Decimal, trigger: Decimal, link_id: str
    ) -> bool:
        ex_side = ExSide.SELL if side is Side.LONG else ExSide.BUY
        try:
            inst = await self.client.instrument(symbol)
            if inst is None:
                return False
            await self.client.place_order(
                OrderRequest(
                    symbol=symbol,
                    side=ex_side,
                    qty=inst.round_qty(qty),
                    # Round away from the market: the backup must never sit inside the
                    # bot's own stop.
                    trigger_price=inst.round_price(trigger, up=ex_side is ExSide.BUY),
                    order_link_id=link_id,
                    is_leverage=False,
                )
            )
        except BybitError as exc:
            log.error("%s: backup stop not placed: %s", symbol, exc)
            return False
        return True

    async def cancel_backup_stop(self, symbol: str, link_id: str) -> bool:
        try:
            await self.client.cancel_order(symbol, link_id, STOP_FILTER)
        except BybitAPIError as exc:
            if exc.ret_code in ORDER_NOT_FOUND:
                return True
            log.error("%s: backup stop %s not cancelled: %s", symbol, link_id, exc)
            return False
        except BybitError as exc:
            log.error("%s: backup stop %s not cancelled: %s", symbol, link_id, exc)
            return False
        return True

    async def backup_stop_fill(self, symbol: str, link_id: str) -> OrderOutcome | None:
        try:
            order = await self.client.order_history(symbol, link_id)
        except BybitError as exc:
            return OrderOutcome(Outcome.UNREACHABLE, detail=str(exc)[:200])
        if order is None or order.cum_exec_qty <= 0:
            return None
        filled, avg, fee = _fill_of(order)
        outcome = Outcome.FILLED if filled >= order.qty else Outcome.PARTIAL
        return OrderOutcome(outcome, filled, avg, fee, "exchange-side backup stop")


def _fill_of(order: Order | None) -> tuple[Decimal, Decimal | None, Decimal]:
    if order is None:
        return Decimal(0), None, Decimal(0)
    return order.cum_exec_qty, order.avg_price or None, order.cum_exec_fee or Decimal(0)


def _never_placed(symbol: str, link_id: str) -> Order:
    return Order(
        order_id="",
        order_link_id=link_id,
        symbol=symbol,
        side=ExSide.SELL,
        qty=Decimal(0),
        order_status="Rejected",
    )


def _is_dust(inst: Instrument, qty: Decimal, price: Decimal) -> bool:
    lot = inst.lot_size_filter
    return qty < lot.min_order_qty or qty * price < lot.min_order_amt
