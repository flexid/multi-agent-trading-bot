"""Thin async client for the Bybit V5 REST API (spot and spot margin only).

Signing follows the V5 auth docs: HMAC-SHA256 over
``timestamp + api_key + recv_window + (query string | JSON body)``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from app.execution.bybit_models import (
    AccountInfo,
    ApiKeyInfo,
    FeeRate,
    Instrument,
    Kline,
    MarginCoin,
    Order,
    OrderAck,
    OrderBook,
    OrderRequest,
    SpotMarginState,
    Ticker,
    WalletBalance,
)

CATEGORY = "spot"


class BybitError(Exception):
    """Base class for client errors."""


class BybitHTTPError(BybitError):
    def __init__(self, path: str, status: int) -> None:
        super().__init__(f"{path}: HTTP {status}")
        self.path = path
        self.status = status


class BybitAPIError(BybitError):
    def __init__(self, path: str, ret_code: int, ret_msg: str) -> None:
        super().__init__(f"{path}: retCode {ret_code}: {ret_msg}")
        self.path = path
        self.ret_code = ret_code
        self.ret_msg = ret_msg


class BybitTransportError(BybitError):
    """The request did not complete: timeout, connection reset, DNS. For an order this
    means "unknown", not "not placed": the exchange may have received it."""

    def __init__(self, path: str, cause: Exception) -> None:
        super().__init__(f"{path}: {type(cause).__name__}: {cause}")
        self.path = path


class OrdersDisabledError(BybitError):
    """Raised when an order call is made on a client not built for trading."""


def sign(secret: str, timestamp_ms: int, api_key: str, recv_window_ms: int, payload: str) -> str:
    message = f"{timestamp_ms}{api_key}{recv_window_ms}{payload}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


RATE_LIMITED = 10006  # "Too many visits": the margin-data endpoint trips this easily
RATE_LIMIT_BACKOFF_S: tuple[float, ...] = (1.0, 3.0)


class BybitClient:
    def __init__(
        self,
        base_url: str,
        api_key: SecretStr | None = None,
        api_secret: SecretStr | None = None,
        *,
        recv_window_ms: int = 5000,
        timeout_s: float = 10.0,
        allow_orders: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._secret = api_secret
        self._recv_window_ms = recv_window_ms
        self._allow_orders = allow_orders
        self._time_offset_ms = 0
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def has_credentials(self) -> bool:
        return bool(
            self._key
            and self._secret
            and self._key.get_secret_value()
            and self._secret.get_secret_value()
        )

    # --- transport -------------------------------------------------------

    def _auth_headers(self, payload: str) -> dict[str, str]:
        if not self.has_credentials:
            raise BybitError("authenticated endpoint called without API credentials")
        assert self._key is not None and self._secret is not None
        key = self._key.get_secret_value()
        ts = int(time.time() * 1000) + self._time_offset_ms
        return {
            "X-BAPI-API-KEY": key,
            "X-BAPI-TIMESTAMP": str(ts),
            "X-BAPI-RECV-WINDOW": str(self._recv_window_ms),
            "X-BAPI-SIGN": sign(
                self._secret.get_secret_value(), ts, key, self._recv_window_ms, payload
            ),
        }

    @staticmethod
    def _unwrap(path: str, response: httpx.Response) -> dict[str, Any]:
        if response.status_code != 200:
            raise BybitHTTPError(path, response.status_code)
        body = response.json()
        if body.get("retCode") != 0:
            raise BybitAPIError(path, int(body.get("retCode", -1)), str(body.get("retMsg", "")))
        result = body.get("result")
        return result if isinstance(result, dict) else {}

    async def _get(
        self, path: str, params: dict[str, Any] | None = None, *, auth: bool = False
    ) -> dict[str, Any]:
        query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{path}?{query}" if query else path
        # Reads are safe to repeat: one retry on a transport failure, a short back-off
        # on the rate limit (10006), then give up.
        for attempt, pause in enumerate(RATE_LIMIT_BACKOFF_S + (None,), start=1):
            try:
                headers = self._auth_headers(query) if auth else {}
                return self._unwrap(path, await self._http.get(url, headers=headers))
            except httpx.TransportError as exc:
                if attempt >= 2:
                    raise BybitTransportError(path, exc) from exc
            except BybitAPIError as exc:
                if exc.ret_code != RATE_LIMITED or pause is None:
                    raise
                await asyncio.sleep(pause)
        raise AssertionError("unreachable")

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        raw = json.dumps(body, separators=(",", ":"))
        headers = {**self._auth_headers(raw), "Content-Type": "application/json"}
        try:
            response = await self._http.post(path, content=raw, headers=headers)
        except httpx.TransportError as exc:  # writes are never retried here
            raise BybitTransportError(path, exc) from exc
        return self._unwrap(path, response)

    # --- public market data ----------------------------------------------

    async def server_time(self) -> datetime:
        result = await self._get("/v5/market/time")
        return datetime.fromtimestamp(int(result["timeNano"]) / 1e9, tz=UTC)

    async def sync_time(self) -> int:
        """Align request timestamps with the exchange clock. Returns local skew in ms."""
        before = time.time()
        server = await self.server_time()
        local = (before + time.time()) / 2
        self._time_offset_ms = int((server.timestamp() - local) * 1000)
        return -self._time_offset_ms

    async def instrument(self, symbol: str) -> Instrument | None:
        result = await self._get(
            "/v5/market/instruments-info", {"category": CATEGORY, "symbol": symbol}
        )
        rows = result.get("list") or []
        return Instrument.model_validate(rows[0]) if rows else None

    async def klines(
        self, symbol: str, interval: str, limit: int = 200, end: datetime | None = None
    ) -> list[Kline]:
        """Candles, newest first as Bybit returns them. ``interval``: 15, 60, 240 or D.
        ``end``: the newest candle to return (paging backwards for a backfill)."""
        result = await self._get(
            "/v5/market/kline",
            {
                "category": CATEGORY,
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "end": int(end.timestamp() * 1000) if end else None,
            },
        )
        return [Kline.from_row(row) for row in result.get("list") or []]

    async def ticker(self, symbol: str) -> Ticker:
        result = await self._get("/v5/market/tickers", {"category": CATEGORY, "symbol": symbol})
        return Ticker.model_validate(result["list"][0])

    async def orderbook(self, symbol: str, limit: int = 200) -> OrderBook:
        result = await self._get(
            "/v5/market/orderbook", {"category": CATEGORY, "symbol": symbol, "limit": limit}
        )
        return OrderBook.model_validate(result)

    async def margin_coin(self, coin: str, vip_level: str = "No VIP") -> MarginCoin | None:
        """Public borrow terms for one coin; None when the coin cannot be margined."""
        result = await self._get(
            "/v5/spot-margin-trade/data", {"currency": coin, "vipLevel": vip_level}
        )
        for tier in result.get("vipCoinList") or []:
            for row in tier.get("list") or []:
                if row.get("currency") == coin:
                    return MarginCoin.model_validate(row)
        return None

    # --- account (signed reads) ------------------------------------------

    async def api_key_info(self) -> ApiKeyInfo:
        return ApiKeyInfo.model_validate(await self._get("/v5/user/query-api", auth=True))

    async def account_info(self) -> AccountInfo:
        return AccountInfo.model_validate(await self._get("/v5/account/info", auth=True))

    async def wallet_balance(self) -> WalletBalance:
        result = await self._get(
            "/v5/account/wallet-balance", {"accountType": "UNIFIED"}, auth=True
        )
        return WalletBalance.model_validate(result["list"][0])

    async def repay(self, coin: str) -> Decimal:
        """Repay the UTA liability in ``coin`` from the balance held; the quantity repaid.
        Bybit keeps a spot-margin borrow on the books after the coin is bought back."""
        result = await self._post("/v5/account/quick-repayment", {"coin": coin})
        return sum(
            (Decimal(str(row["repaymentQty"])) for row in result.get("list", [])), Decimal(0)
        )

    async def fee_rate(self, symbol: str) -> FeeRate:
        result = await self._get(
            "/v5/account/fee-rate", {"category": CATEGORY, "symbol": symbol}, auth=True
        )
        return FeeRate.model_validate(result["list"][0])

    async def spot_margin_state(self) -> SpotMarginState:
        return SpotMarginState.model_validate(
            await self._get("/v5/spot-margin-trade/state", auth=True)
        )

    async def sub_member_count(self) -> int:
        result = await self._get("/v5/user/query-sub-members", auth=True)
        return len(result.get("subMembers") or [])

    async def open_orders(
        self, symbol: str | None = None, order_filter: str | None = None
    ) -> list[Order]:
        """Resting orders. Conditional stops only show with ``order_filter="StopOrder"``."""
        result = await self._get(
            "/v5/order/realtime",
            {"category": CATEGORY, "symbol": symbol, "orderFilter": order_filter},
            auth=True,
        )
        return [Order.model_validate(row) for row in result.get("list") or []]

    async def order_history(self, symbol: str, order_link_id: str) -> Order | None:
        """One of our orders by ``orderLinkId``; None when the exchange does not know it."""
        result = await self._get(
            "/v5/order/history",
            {"category": CATEGORY, "symbol": symbol, "orderLinkId": order_link_id},
            auth=True,
        )
        rows = result.get("list") or []
        return Order.model_validate(rows[0]) if rows else None

    async def find_order(self, symbol: str, order_link_id: str) -> Order | None:
        """Our order by ``orderLinkId``, live or just finished. The realtime endpoint
        answers at once and also returns recently closed orders; history is the fallback."""
        result = await self._get(
            "/v5/order/realtime",
            {"category": CATEGORY, "symbol": symbol, "orderLinkId": order_link_id},
            auth=True,
        )
        rows = result.get("list") or []
        if rows:
            return Order.model_validate(rows[0])
        return await self.order_history(symbol, order_link_id)

    # --- orders ----------------------------------------------------------

    def _require_orders(self) -> None:
        if not self._allow_orders:
            raise OrdersDisabledError("this client was not created with allow_orders=True")

    async def place_order(self, request: OrderRequest) -> OrderAck:
        self._require_orders()
        return OrderAck.model_validate(await self._post("/v5/order/create", request.payload()))

    async def cancel_order(
        self, symbol: str, order_link_id: str, order_filter: str | None = None
    ) -> OrderAck:
        self._require_orders()
        body = {"category": CATEGORY, "symbol": symbol, "orderLinkId": order_link_id}
        if order_filter:
            body["orderFilter"] = order_filter
        return OrderAck.model_validate(await self._post("/v5/order/cancel", body))
