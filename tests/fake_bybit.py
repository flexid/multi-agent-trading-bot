"""A scripted Bybit for the live-gateway tests: just enough of the V5 spot API to drive
``BybitGateway`` over ``httpx.MockTransport``. No network, no real orders.

- post-only limits fill in full at their price (the entry path is not under test here)
- IOC limits fill at the touch when they cross it, up to ``ioc_liquidity`` per order
  (a list consumed one entry per order; empty = unlimited), the rest is cancelled
- conditional stops rest until ``trigger_stop`` fills them or a cancel removes them
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
from pydantic import SecretStr

from app.execution.bybit_client import BybitClient

D = Decimal


class FakeBybit:
    def __init__(self, symbol: str, bid: str, ask: str, tick: str = "0.1") -> None:
        self.symbol = symbol
        self.bid, self.ask = D(bid), D(ask)
        self.tick = tick
        self.orders: dict[str, dict[str, Any]] = {}  # every order by orderLinkId
        self.created: list[dict[str, Any]] = []  # create payloads, in order
        self.calls: list[str] = []  # "create:<tif>", "cancel:<link>", in order
        self.ioc_liquidity: list[D] = []
        self.drop_next_create = False  # the request reaches the exchange, the reply is lost
        self.fail_cancel = False
        self.holdings: dict[str, D] = {}  # base-coin balance; empty = plenty (not under test)

    def client(self) -> BybitClient:
        return BybitClient(
            "https://bybit.test",
            SecretStr("KEY"),
            SecretStr("SECRET"),
            allow_orders=True,
            transport=httpx.MockTransport(self.handle),
        )

    def move(self, bid: str, ask: str) -> None:
        self.bid, self.ask = D(bid), D(ask)

    def trigger_stop(self, link: str, price: str) -> None:
        order = self.orders[link]
        order |= {"orderStatus": "Filled", "cumExecQty": order["qty"], "avgPrice": price}

    def resting_stops(self) -> list[dict[str, Any]]:
        return [o for o in self.orders.values() if o["orderStatus"] == "Untriggered"]

    # --- transport ------------------------------------------------------------------

    @staticmethod
    def _ok(result: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})

    @staticmethod
    def _err(code: int, msg: str) -> httpx.Response:
        return httpx.Response(200, json={"retCode": code, "retMsg": msg, "result": {}})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, q = request.url.path, dict(request.url.params)
        if path == "/v5/market/instruments-info":
            return self._ok({"list": [self._instrument()]})
        if path == "/v5/market/tickers":
            return self._ok({"list": [self._ticker()]})
        if path in ("/v5/order/realtime", "/v5/order/history"):
            return self._ok({"list": self._query(path, q)})
        if path == "/v5/account/wallet-balance":
            base = self.symbol.replace("USDT", "")
            held = self.holdings.get(base, D("1000000"))
            return self._ok(
                {
                    "list": [
                        {
                            "accountType": "UNIFIED",
                            "totalEquity": "0",
                            "coin": [{"coin": base, "walletBalance": str(held)}],
                        }
                    ]
                }
            )
        body = json.loads(request.content)
        if path == "/v5/order/create":
            return self._create(body)
        if path == "/v5/order/cancel":
            return self._cancel(body)
        raise AssertionError(f"unexpected call {path}")

    def _instrument(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "baseCoin": "BTC",
            "quoteCoin": "USDT",
            "status": "Trading",
            "marginTrading": "utaOnly",
            "lotSizeFilter": {
                "basePrecision": "0.001",
                "quotePrecision": "0.01",
                "minOrderQty": "0.001",
                "maxOrderQty": "1000",
                "minOrderAmt": "1",
                "maxOrderAmt": "1000000",
            },
            "priceFilter": {"tickSize": self.tick},
        }

    def _ticker(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bid1Price": str(self.bid),
            "bid1Size": "10",
            "ask1Price": str(self.ask),
            "ask1Size": "10",
            "lastPrice": str(self.bid),
            "turnover24h": "1000000",
            "volume24h": "10000",
        }

    def _query(self, path: str, q: dict[str, str]) -> list[dict[str, Any]]:
        link = q.get("orderLinkId")
        if link:
            order = self.orders.get(link)
            if order is None or (
                path.endswith("history") and order["orderStatus"] == "Untriggered"
            ):
                return []
            return [order]
        if q.get("orderFilter") == "StopOrder":
            return self.resting_stops()
        return []  # nothing else ever rests: limits fill or cancel at once

    def _create(self, body: dict[str, Any]) -> httpx.Response:
        self.created.append(body)
        link, qty = body["orderLinkId"], D(body["qty"])
        order: dict[str, Any] = {
            "orderId": f"id-{len(self.created)}",
            "orderLinkId": link,
            "symbol": body["symbol"],
            "side": body["side"],
            "price": body.get("price", ""),
            "qty": body["qty"],
            "cumExecQty": "0",
            "avgPrice": "",
            "cumExecFee": "0",
        }
        if body.get("orderFilter") == "StopOrder":
            self.calls.append("create:stop")
            order |= {"orderStatus": "Untriggered", "triggerPrice": body["triggerPrice"]}
        else:
            tif = body["timeInForce"]
            self.calls.append(f"create:{tif}")
            price = D(body["price"])
            if tif == "PostOnly":
                filled, at = qty, price
            else:
                selling = body["side"] == "Sell"
                crosses = price <= self.bid if selling else price >= self.ask
                room = self.ioc_liquidity.pop(0) if self.ioc_liquidity else qty
                filled = min(qty, room) if crosses else D(0)
                at = self.bid if selling else self.ask
            status = (
                "Filled" if filled >= qty else "PartiallyFilledCanceled" if filled else "Cancelled"
            )
            order |= {
                "orderStatus": status,
                "cumExecQty": str(filled),
                "avgPrice": str(at) if filled else "",
                "cumExecFee": str(filled * at * D("0.001")),
            }
        self.orders[link] = order
        if self.drop_next_create:
            self.drop_next_create = False
            raise httpx.ReadTimeout("reply lost")
        return self._ok({"orderId": order["orderId"], "orderLinkId": link})

    def _cancel(self, body: dict[str, Any]) -> httpx.Response:
        link = body["orderLinkId"]
        self.calls.append(f"cancel:{link}")
        if self.fail_cancel:
            raise httpx.ConnectError("down")
        order = self.orders.get(link)
        if order is None or order["orderStatus"] != "Untriggered":
            return self._err(170213, "Order does not exist.")
        order["orderStatus"] = "Deactivated"
        return self._ok({"orderId": order["orderId"], "orderLinkId": link})
