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


def test_digitalocean_metrics_become_samples() -> None:
    """CPU from the cumulative counters, memory from what is still available, disk from
    the root filesystem; the token is the scheduler's only."""
    from pathlib import Path

    from app.data.digitalocean import combine, cpu_usage, used_pct

    def series(labels: dict[str, str], values: list[tuple[int, float]]) -> dict[str, object]:
        return {"metric": {"host_id": "1", **labels}, "values": [[t, str(v)] for t, v in values]}

    cpu = [
        series({"mode": "idle"}, [(100, 1000.0), (220, 1180.0), (340, 1240.0)]),
        series({"mode": "user"}, [(100, 50.0), (220, 100.0), (340, 220.0)]),
        series({"mode": "system"}, [(100, 10.0), (220, 20.0), (340, 80.0)]),
    ]
    # 2 cores × 120 s = 240 core-seconds per interval: 60 busy → 25%, then 180 busy → 75%
    assert cpu_usage(cpu) == {220: 25.0, 340: 75.0}
    assert used_pct({1: 25.0}, {1: 100.0}) == {1: 75.0}
    samples = combine(
        {
            "cpu": cpu,
            "memory_total": [series({}, [(220, 4000.0), (340, 4000.0)])],
            "memory_available": [series({}, [(220, 3000.0), (340, 1000.0)])],
            "filesystem_size": [
                series({"mountpoint": "/"}, [(340, 80.0)]),
                series({"mountpoint": "/boot"}, [(340, 1.0)]),
            ],
            "filesystem_free": [
                series({"mountpoint": "/"}, [(340, 16.0)]),
                series({"mountpoint": "/boot"}, [(340, 1.0)]),
            ],
            "load_1": [series({}, [(340, 0.5)])],
            "bandwidth_out": [series({}, [(340, 0.04)])],
        }
    )
    assert [s.cpu_pct for s in samples] == [25.0, 75.0]
    last = samples[-1]
    assert (last.mem_used_pct, last.disk_used_pct, last.load_1, last.net_out_mbps) == (
        75.0,
        80.0,
        0.5,
        0.04,
    )
    assert last.mem_total == 4000 and last.disk_total == 80 and last.net_in_mbps is None
    assert combine({}) == []
    assert "DO_ACCESS" not in Path("scripts/admin_env.sh").read_text()


def test_sparkline_scales_to_the_ceiling_and_skips_gaps() -> None:
    from app.admin.app import sparkline

    assert sparkline([0.0, 50.0, 100.0], top=100, w=100, h=10) == "0.0,10.0 50.0,5.0 100.0,0.0"
    assert sparkline([0.0, None, 200.0], top=100, w=100, h=10) == "0.0,10.0 100.0,0.0"
    assert sparkline([5.0]) == "" and sparkline([]) == ""
