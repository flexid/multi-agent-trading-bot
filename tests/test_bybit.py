import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.execution.bybit_client import (
    BybitAPIError,
    BybitClient,
    BybitError,
    BybitHTTPError,
    OrdersDisabledError,
    sign,
)
from app.execution.bybit_models import Instrument, OrderBook, OrderRequest, Side

INSTRUMENT: dict[str, Any] = {
    "symbol": "BTCUSDC",
    "baseCoin": "BTC",
    "quoteCoin": "USDC",
    "status": "Trading",
    "marginTrading": "utaOnly",
    "lotSizeFilter": {
        "basePrecision": "0.000001",
        "quotePrecision": "0.0000001",
        "minOrderQty": "0.000001",
        "maxOrderQty": "273.9",
        "minOrderAmt": "5",
        "maxOrderAmt": "1200000",
    },
    "priceFilter": {"tickSize": "0.1"},
}


def ok(result: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})


def client_for(handler: Any, **kwargs: Any) -> BybitClient:
    return BybitClient(
        "https://bybit.test",
        SecretStr("KEY"),
        SecretStr("SECRET"),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def order() -> OrderRequest:
    return OrderRequest(
        symbol="BTCUSDC",
        side=Side.BUY,
        qty=Decimal("0.000070"),
        price=Decimal("73742.1"),
        order_link_id="selftest-abc",
        post_only=True,
    )


def test_sign_matches_reference_hmac() -> None:
    # HMAC-SHA256("SECRET", "1700000000000KEY5000category=spot")
    assert sign("SECRET", 1700000000000, "KEY", 5000, "category=spot") == (
        "e59ffb06113567731396e9bfd0a0cf421e21ae19bbcd476f6db0296733678a0a"
    )


def test_instrument_rounding_uses_exchange_filters() -> None:
    inst = Instrument.model_validate(INSTRUMENT)
    assert inst.round_price(Decimal("73742.19")) == Decimal("73742.1")
    assert inst.round_price(Decimal("73742.11"), up=True) == Decimal("73742.2")
    assert inst.round_qty(Decimal("0.0000019")) == Decimal("0.000001")
    assert inst.margin_enabled
    qty = inst.min_qty_at(Decimal("73742.1"))
    assert qty * Decimal("73742.1") >= Decimal("5")
    assert qty == inst.round_qty(qty)


def test_margin_none_means_no_borrowing() -> None:
    inst = Instrument.model_validate({**INSTRUMENT, "marginTrading": "none"})
    assert not inst.margin_enabled


def test_orderbook_depth_within_band() -> None:
    book = OrderBook.model_validate(
        {
            "s": "BTCUSDC",
            "b": [["99", "1"], ["97", "10"]],
            "a": [["101", "2"], ["103", "10"]],
            "ts": 1,
        }
    )
    assert book.mid == Decimal("100")
    assert book.depth_quote(Decimal("0.02")) == (Decimal("99"), Decimal("202"))


def test_order_payload_is_spot_limit_with_plain_decimals() -> None:
    payload = order().payload()
    assert payload["category"] == "spot"
    assert payload["orderType"] == "Limit"
    assert payload["qty"] == "0.000070"
    assert payload["timeInForce"] == "PostOnly"
    assert payload["isLeverage"] == 0


def test_order_link_id_is_validated() -> None:
    with pytest.raises(ValidationError):
        OrderRequest(
            symbol="BTCUSDC", side=Side.BUY, qty=Decimal(1), price=Decimal(1), order_link_id=""
        )


async def test_signed_get_sends_valid_signature() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["query"] = request.url.query.decode()
        return ok(
            {"list": [{"symbol": "BTCUSDC", "takerFeeRate": "0.001", "makerFeeRate": "0.001"}]}
        )

    async with client_for(handler) as client:
        fee = await client.fee_rate("BTCUSDC")

    assert fee.taker_fee_rate == Decimal("0.001")
    headers = seen["headers"]
    assert headers["X-BAPI-API-KEY"] == "KEY"
    expected = sign("SECRET", int(headers["X-BAPI-TIMESTAMP"]), "KEY", 5000, seen["query"])
    assert headers["X-BAPI-SIGN"] == expected
    assert seen["query"] == "category=spot&symbol=BTCUSDC"


async def test_api_error_and_http_error_are_raised() -> None:
    def invalid_key(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"retCode": 10003, "retMsg": "API key is invalid."})

    async with client_for(invalid_key) as client:
        with pytest.raises(BybitAPIError) as err:
            await client.api_key_info()
    assert err.value.ret_code == 10003
    assert "KEY" not in str(err.value).replace("API key", "")

    async with client_for(lambda _: httpx.Response(403)) as client:
        with pytest.raises(BybitHTTPError):
            await client.server_time()


async def test_signed_call_without_credentials_fails_before_any_request() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    async with BybitClient("https://bybit.test", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BybitError):
            await client.wallet_balance()


async def test_orders_are_refused_unless_explicitly_allowed() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    async with client_for(handler) as client:
        with pytest.raises(OrdersDisabledError):
            await client.place_order(order())
        with pytest.raises(OrdersDisabledError):
            await client.cancel_order("BTCUSDC", "selftest-abc")


async def test_place_order_signs_the_exact_body() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        seen["headers"] = request.headers
        return ok({"orderId": "1", "orderLinkId": "selftest-abc"})

    async with client_for(handler, allow_orders=True) as client:
        ack = await client.place_order(order())

    assert ack.order_link_id == "selftest-abc"
    assert json.loads(seen["body"])["orderLinkId"] == "selftest-abc"
    headers = seen["headers"]
    assert headers["X-BAPI-SIGN"] == sign(
        "SECRET", int(headers["X-BAPI-TIMESTAMP"]), "KEY", 5000, seen["body"]
    )


async def test_missing_instrument_returns_none() -> None:
    async with client_for(lambda _: ok({"category": "spot", "list": []})) as client:
        assert await client.instrument("NOPEUSDC") is None


async def test_margin_coin_absent_when_exchange_returns_null() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"retCode": 0, "retMsg": "success", "result": None})

    async with client_for(handler) as client:
        assert await client.margin_coin("SPX") is None
