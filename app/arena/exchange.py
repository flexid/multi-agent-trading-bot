"""The linear (USDT perpetual) adapter over the shared Bybit client, one per trader.

Everything a trader does on the exchange goes through here: leverage, a market entry with
the take-profit and stop-loss attached, a market close, the position, the closed trades
and the account's equity. The exchange runs the take and the stop; the bot only ever
places, amends or closes. ``live=False`` builds the paper twin (app/arena/paper.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from app.execution.bybit_client import BybitAPIError, BybitClient

CATEGORY = "linear"
LEVERAGE_UNCHANGED = 110043  # set-leverage with the same value


@dataclass(frozen=True)
class Instrument:
    tick: Decimal
    lot: Decimal
    min_qty: Decimal


@dataclass(frozen=True)
class Position:
    side: str  # long | short
    qty: Decimal
    entry: Decimal
    leverage: int
    tp: Decimal | None
    sl: Decimal | None
    unrealized_pnl: Decimal
    margin: Decimal


@dataclass(frozen=True)
class ClosedTrade:
    """One closed position as the exchange books it: pnl net of both fee legs."""

    order_id: str
    side: str  # the position's side
    qty: Decimal
    entry: Decimal
    exit: Decimal
    leverage: int
    fees: Decimal
    pnl: Decimal  # net of fees
    closed_at: datetime


class Exchange(Protocol):
    symbol: str

    async def instrument(self) -> Instrument: ...
    async def klines(self, interval: str, limit: int = 151) -> list[list[float]]: ...
    async def last_price(self) -> Decimal: ...
    async def equity(self) -> tuple[Decimal, Decimal]: ...
    async def position(self) -> Position | None: ...
    async def set_leverage(self, leverage: int) -> None: ...
    async def open_market(
        self, side: str, qty: Decimal, tp: Decimal, sl: Decimal, link_id: str
    ) -> str: ...
    async def set_tpsl(self, tp: Decimal, sl: Decimal) -> None: ...
    async def close_market(self, link_id: str) -> None: ...
    async def closed_trades(self, since: datetime) -> list[ClosedTrade]: ...


def _f(x: Any) -> float:
    return float(x) if x not in (None, "") else 0.0


def _d(x: Any) -> Decimal:
    return Decimal(str(x)) if x not in (None, "") else Decimal(0)


class LinearExchange:
    """Real orders on one AI sub-account. The client must have been created with
    ``allow_orders=True``; the arena process checks ``arena.live`` before that."""

    def __init__(self, client: BybitClient, symbol: str) -> None:
        self._c = client
        self.symbol = symbol
        self._instrument: Instrument | None = None

    async def instrument(self) -> Instrument:
        if self._instrument is None:
            r = await self._c._get(
                "/v5/market/instruments-info", {"category": CATEGORY, "symbol": self.symbol}
            )
            row = r["list"][0]
            self._instrument = Instrument(
                tick=_d(row["priceFilter"]["tickSize"]),
                lot=_d(row["lotSizeFilter"]["qtyStep"]),
                min_qty=_d(row["lotSizeFilter"]["minOrderQty"]),
            )
        return self._instrument

    async def klines(self, interval: str, limit: int = 151) -> list[list[float]]:
        """Closed candles, oldest first, as [ms, open, high, low, close, volume]. Bybit
        returns newest first with the candle in progress at the top; that one is dropped."""
        r = await self._c._get(
            "/v5/market/kline",
            {"category": CATEGORY, "symbol": self.symbol, "interval": interval, "limit": limit},
        )
        rows = r.get("list", [])
        closed = rows[1:] if rows else []
        return [
            [float(x[0]), _f(x[1]), _f(x[2]), _f(x[3]), _f(x[4]), _f(x[5])]
            for x in reversed(closed)
        ]

    async def last_price(self) -> Decimal:
        r = await self._c._get("/v5/market/tickers", {"category": CATEGORY, "symbol": self.symbol})
        return _d(r["list"][0]["lastPrice"])

    async def equity(self) -> tuple[Decimal, Decimal]:
        """(total equity, USDT available for new margin), from the unified wallet."""
        r = await self._c._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"}, auth=True)
        row = r["list"][0]
        usdt: dict[str, Any] = next((c for c in row.get("coin", []) if c.get("coin") == "USDT"), {})
        available = _d(row.get("totalAvailableBalance")) or _d(usdt.get("availableToWithdraw"))
        return _d(row.get("totalEquity")), available

    async def position(self) -> Position | None:
        r = await self._c._get(
            "/v5/position/list", {"category": CATEGORY, "symbol": self.symbol}, auth=True
        )
        for row in r.get("list", []):
            if _d(row.get("size")) > 0:
                side = "long" if row.get("side") == "Buy" else "short"
                return Position(
                    side=side,
                    qty=_d(row["size"]),
                    entry=_d(row.get("avgPrice")),
                    leverage=int(_f(row.get("leverage")) or 1),
                    tp=_d(row.get("takeProfit")) or None,
                    sl=_d(row.get("stopLoss")) or None,
                    unrealized_pnl=_d(row.get("unrealisedPnl")),
                    margin=_d(row.get("positionIM")) or _d(row.get("positionBalance")),
                )
        return None

    async def set_leverage(self, leverage: int) -> None:
        try:
            await self._c._post(
                "/v5/position/set-leverage",
                {
                    "category": CATEGORY,
                    "symbol": self.symbol,
                    "buyLeverage": str(leverage),
                    "sellLeverage": str(leverage),
                },
            )
        except BybitAPIError as exc:
            if exc.ret_code != LEVERAGE_UNCHANGED:
                raise

    async def open_market(
        self, side: str, qty: Decimal, tp: Decimal, sl: Decimal, link_id: str
    ) -> str:
        """A market entry with the take and the stop attached (whole position, market
        exits), idempotent on ``link_id``. Returns the exchange order id."""
        r = await self._c._post(
            "/v5/order/create",
            {
                "category": CATEGORY,
                "symbol": self.symbol,
                "side": "Buy" if side == "long" else "Sell",
                "orderType": "Market",
                "qty": str(qty),
                "timeInForce": "IOC",
                "positionIdx": 0,
                "orderLinkId": link_id,
                "takeProfit": str(tp),
                "stopLoss": str(sl),
                "tpslMode": "Full",
                "tpOrderType": "Market",
                "slOrderType": "Market",
                "tpTriggerBy": "LastPrice",
                "slTriggerBy": "LastPrice",
            },
        )
        return str(r.get("orderId", ""))

    async def set_tpsl(self, tp: Decimal, sl: Decimal) -> None:
        """Re-set the take and the stop from the real fill price."""
        await self._c._post(
            "/v5/position/trading-stop",
            {
                "category": CATEGORY,
                "symbol": self.symbol,
                "positionIdx": 0,
                "tpslMode": "Full",
                "takeProfit": str(tp),
                "stopLoss": str(sl),
                "tpTriggerBy": "LastPrice",
                "slTriggerBy": "LastPrice",
            },
        )

    async def close_market(self, link_id: str) -> None:
        pos = await self.position()
        if pos is None:
            return
        await self._c._post(
            "/v5/order/create",
            {
                "category": CATEGORY,
                "symbol": self.symbol,
                "side": "Sell" if pos.side == "long" else "Buy",
                "orderType": "Market",
                "qty": str(pos.qty),
                "timeInForce": "IOC",
                "positionIdx": 0,
                "reduceOnly": True,
                "orderLinkId": link_id,
            },
        )

    async def closed_trades(self, since: datetime) -> list[ClosedTrade]:
        """Closed positions since ``since``, oldest first. Bybit's closedPnl is net of both
        fee legs (openFee and closeFee are reported beside it)."""
        r = await self._c._get(
            "/v5/position/closed-pnl",
            {
                "category": CATEGORY,
                "symbol": self.symbol,
                "startTime": int(since.timestamp() * 1000),
                "limit": 50,
            },
            auth=True,
        )
        out = []
        for row in r.get("list", []):
            fees = _d(row.get("openFee")) + _d(row.get("closeFee"))
            out.append(
                ClosedTrade(
                    order_id=str(row.get("orderId", "")),
                    side="short"
                    if row.get("side") == "Buy"
                    else "long",  # the closing order's side
                    qty=_d(row.get("qty")),
                    entry=_d(row.get("avgEntryPrice")),
                    exit=_d(row.get("avgExitPrice")),
                    leverage=int(_f(row.get("leverage")) or 1),
                    fees=fees,
                    pnl=_d(row.get("closedPnl")),
                    closed_at=datetime.fromtimestamp(int(row["updatedTime"]) / 1000, tz=UTC),
                )
            )
        return sorted(out, key=lambda t: t.closed_at)
