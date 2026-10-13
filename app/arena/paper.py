"""The paper twin of the linear adapter: real prices and candles from the public endpoints,
simulated fills, the take and the stop checked against every closed 1-minute candle.
Fees as on the real account. Used while the arena runs without funds (``arena.live =
false``) and by the tests, which never touch an order endpoint."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from app.arena.exchange import ClosedTrade, Instrument, Position


class PaperExchange:
    def __init__(
        self,
        symbol: str,
        *,
        equity: Decimal,
        taker_fee_pct: Decimal,
        prices: PriceSource,
    ) -> None:
        self.symbol = symbol
        self.cash = equity
        self.fee = taker_fee_pct / 100
        self._prices = prices
        self._leverage = 1
        self._pos: Position | None = None
        self._opened_at: datetime | None = None
        self._open_fee = Decimal(0)
        self._closed: list[ClosedTrade] = []
        self.now: datetime | None = None  # the engine's clock; real time when unset
        self.instrument_spec = Instrument(
            tick=Decimal("0.1"), lot=Decimal("0.001"), min_qty=Decimal("0.001")
        )

    async def instrument(self) -> Instrument:
        return self.instrument_spec

    async def klines(self, interval: str, limit: int = 151) -> list[list[float]]:
        return await self._prices.klines(interval, limit)

    async def last_price(self) -> Decimal:
        return await self._prices.last_price()

    async def equity(self) -> tuple[Decimal, Decimal]:
        if self._pos is None:
            return self.cash, self.cash
        price = await self.last_price()
        unreal = (price - self._pos.entry) * self._pos.qty * (1 if self._pos.side == "long" else -1)
        return self.cash + self._pos.margin + unreal, self.cash

    async def position(self) -> Position | None:
        if self._pos is None:
            return None
        price = await self.last_price()
        unreal = (price - self._pos.entry) * self._pos.qty * (1 if self._pos.side == "long" else -1)
        return Position(**{**self._pos.__dict__, "unrealized_pnl": unreal})

    async def set_leverage(self, leverage: int) -> None:
        self._leverage = leverage

    async def open_market(
        self, side: str, qty: Decimal, tp: Decimal, sl: Decimal, link_id: str
    ) -> str:
        if self._pos is not None:
            raise RuntimeError("paper: a position is already open")
        price = await self.last_price()
        notional = price * qty
        margin = notional / self._leverage
        self._open_fee = notional * self.fee
        self.cash -= margin + self._open_fee
        self._pos = Position(side, qty, price, self._leverage, tp, sl, Decimal(0), margin)
        self._opened_at = self._now()
        return f"paper-{link_id}"

    async def set_tpsl(self, tp: Decimal, sl: Decimal) -> None:
        if self._pos is not None:
            self._pos = Position(**{**self._pos.__dict__, "tp": tp, "sl": sl})

    async def close_market(self, link_id: str) -> None:
        if self._pos is not None:
            await self._settle(await self.last_price(), f"paper-{link_id}")

    async def closed_trades(self, since: datetime) -> list[ClosedTrade]:
        return [t for t in self._closed if t.closed_at >= since]

    def _now(self) -> datetime:
        return self.now or datetime.now(UTC)

    # --- the simulation ----------------------------------------------------------

    async def on_candle(self, high: Decimal, low: Decimal) -> None:
        """Called by the engine after each closed 1-minute candle: the exchange-side take
        and stop fire when the candle crossed them (the stop first when both did)."""
        p = self._pos
        if p is None:
            return
        if p.side == "long":
            if p.sl is not None and low <= p.sl:
                await self._settle(p.sl, "paper-sl")
            elif p.tp is not None and high >= p.tp:
                await self._settle(p.tp, "paper-tp")
        else:
            if p.sl is not None and high >= p.sl:
                await self._settle(p.sl, "paper-sl")
            elif p.tp is not None and low <= p.tp:
                await self._settle(p.tp, "paper-tp")

    async def _settle(self, price: Decimal, order_id: str) -> None:
        p = self._pos
        assert p is not None
        gross = (price - p.entry) * p.qty * (1 if p.side == "long" else -1)
        close_fee = price * p.qty * self.fee
        pnl = gross - self._open_fee - close_fee
        self.cash += p.margin + gross - close_fee
        self._closed.append(
            ClosedTrade(
                order_id=order_id,
                side=p.side,
                qty=p.qty,
                entry=p.entry,
                exit=price,
                leverage=p.leverage,
                fees=self._open_fee + close_fee,
                pnl=pnl,
                closed_at=self._now(),
            )
        )
        self._pos, self._opened_at, self._open_fee = None, None, Decimal(0)


class PriceSource(Protocol):
    """Public market data for the paper twin: the real exchange's candles and last price
    (a LinearExchange serves both), or a scripted series in tests."""

    async def klines(self, interval: str, limit: int = 151) -> list[list[float]]: ...
    async def last_price(self) -> Decimal: ...
