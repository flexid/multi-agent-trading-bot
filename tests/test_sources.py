from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from app.data.polymarket import GammaMarket, PolymarketClient
from app.data.x import XClient, XError, XPost


def test_gamma_json_string_fields_are_parsed() -> None:
    market = GammaMarket.model_validate(
        {
            "id": "1",
            "question": "ignored",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.62", "0.38"]',
            "clobTokenIds": '["111", "222"]',
            "volume24hr": 1234.5,
        }
    )
    assert market.outcomes == ["Yes", "No"]
    assert market.outcome_prices == [Decimal("0.62"), Decimal("0.38")]
    assert market.clob_token_ids == ["111", "222"]
    assert market.question == "ignored"
    assert "ignored" not in repr(market)


async def test_polymarket_client_reads_markets_and_midpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gamma-api.polymarket.com":
            assert request.url.params["closed"] == "false"
            return httpx.Response(200, json=[{"id": "7", "outcomes": '["Yes","No"]'}])
        return httpx.Response(200, json={"mid": "0.615"})

    async with PolymarketClient(transport=httpx.MockTransport(handler)) as pm:
        assert [m.id for m in await pm.top_markets()] == ["7"]
        assert await pm.midpoint("111") == Decimal("0.615")


async def test_x_search_sends_bearer_and_respects_api_minimum() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer TOKEN"
        assert request.url.params["max_results"] == "10"
        return httpx.Response(
            200, json={"data": [{"id": "1", "text": "x", "created_at": "2026-10-08T10:00:00Z"}]}
        )

    async with XClient(SecretStr("TOKEN"), transport=httpx.MockTransport(handler)) as x:
        posts = await x.search_recent("BTC", max_results=1)
    assert len(posts) == 1


async def test_x_error_carries_status_not_token() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"title": "Unauthorized"})

    async with XClient(SecretStr("TOKEN"), transport=httpx.MockTransport(handler)) as x:
        with pytest.raises(XError) as err:
            await x.user_by_username("someone")
    assert err.value.status == 401
    assert "TOKEN" not in str(err.value)


def test_post_text_stays_out_of_repr() -> None:
    assert "untrusted" not in repr(XPost(id="1", text="untrusted words"))
