from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from app.decision import tuning
from app.risk import golive
from tests.test_data_layer import needs_db


def test_sharpe_and_daily_returns() -> None:
    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    series = [
        (t0 + timedelta(hours=6 * i), D(10000) * (1 + D("0.001")) ** (i // 4)) for i in range(60)
    ]
    rets = golive.daily_returns(series)
    assert len(rets) == 14 and all(abs(r - 0.001) < 1e-9 for r in rets)
    assert golive.sharpe(rets, 365) is None  # zero variance → undefined
    assert (
        golive.sharpe([0.01, -0.005, 0.02, 0.0, 0.003, -0.01, 0.015, 0.002, -0.004, 0.006], 365)
        is not None
    )
    assert golive.sharpe([0.01] * 5, 365) is None  # too few


def test_tuning_moves_at_most_five_points_then_shrinks_to_equal() -> None:
    weights = dict(tuning.BASE_WEIGHTS)
    ics: dict[str, float | None] = {
        "indicators": 0.10,
        "chart_patterns": 0.02,
        "polymarket": 0.06,
        "macro": 0.03,
        "x_sentiment": None,  # too few samples: its weight stands
    }
    new = tuning.tune_step(weights, ics, set())
    assert abs(sum(new.values()) - 1) < 1e-3
    assert new["indicators"] > new["polymarket"] > new["chart_patterns"]
    assert all(abs(new[a] - weights[a]) < 0.06 for a in weights)  # ±5 pp, then halved


def test_tuning_never_zeroes_an_agent_and_penalizes_only_after_two_negative_windows() -> None:
    weights = dict(tuning.BASE_WEIGHTS)
    ics: dict[str, float | None] = {
        "indicators": 0.10,
        "chart_patterns": 0.05,
        "polymarket": -0.02,  # negative now, but not in both of the last two windows
        "macro": -0.03,  # negative in both
        "x_sentiment": 0.0,  # IC ≤ 0 without two negative windows
    }
    new = tuning.tune_step(weights, ics, negative_twice={"macro"})
    assert all(w > 0 for w in new.values())  # no instant zero
    mean = sum(v for v in ics.values() if v is not None) / 5
    steps = {
        "indicators": min(tuning.MAX_STEP, 0.10 - mean),
        "chart_patterns": min(tuning.MAX_STEP, 0.05 - mean),
        "polymarket": 0.0,
        "macro": -tuning.MAX_STEP,
        "x_sentiment": 0.0,
    }
    shrunk = {a: (weights[a] + s + 0.2) / 2 for a, s in steps.items()}  # halfway to equal
    total = sum(shrunk.values())
    for agent in weights:
        assert new[agent] == pytest.approx(shrunk[agent] / total, abs=1e-4)
    # At most 5 pp per step, even for the penalized agent.
    assert weights["macro"] - new["macro"] <= tuning.MAX_STEP


def test_capital_ramp_steps_up_after_three_good_weeks_and_back_after_a_pause() -> None:
    start = D("0.10")
    step = golive.next_capital_fraction

    def at(
        current: str, days: float, net: int = 50, paused: bool = False, braked: bool = False
    ) -> D:
        return step(
            D(current),
            start,
            days_in_step=days,
            paused_in_step=paused,
            braked=braked,
            net_in_step=D(net),
        )

    assert golive.capital_steps(start) == [D("0.10"), D("0.25"), D("0.50"), D("1.00")]
    assert at("0.10", 20.9) == D("0.10")  # not yet three weeks
    assert at("0.10", 21) == D("0.25")
    assert at("0.25", 21) == D("0.50")
    assert at("0.50", 30) == D("1.00")
    assert at("1.00", 99) == D("1.00")  # the top
    # Net of ALL costs must be positive: break-even or a loss holds the step.
    assert at("0.25", 40, net=0) == D("0.25")
    assert at("0.25", 40, net=-5) == D("0.25")
    # One step back after a drawdown pause, never below the start, whatever the P&L.
    assert at("0.50", 40, paused=True) == D("0.25")
    assert at("0.10", 40, paused=True) == D("0.10")
    # The emergency brake holds the step; it never raises it.
    assert at("0.25", 40, braked=True) == D("0.25")


def test_running_costs_cover_llm_x_and_server() -> None:
    from app import costs

    t0 = datetime(2026, 10, 1, tzinfo=UTC)
    month = costs.server_cost(t0, t0 + timedelta(days=30.4375))
    assert month == costs.SERVER_USD_PER_MONTH
    c = costs.RunningCosts(llm=D("120.50"), x_reads=600, x_posts=20, server=month)
    assert c.x == D("3.300")  # 600 × 0.005 + 20 × 0.015
    assert c.total == D("120.50") + D("3.300") + D(24)


@needs_db
def test_golive_judges_net_after_all_costs_and_requires_the_websocket_feed() -> None:
    from app.config import get_config
    from app.db.session import new_session

    cfg = get_config()
    when = datetime(2026, 10, 9, tzinfo=UTC)
    with new_session() as s:
        verdict = golive.evaluate(s, cfg, when)
    names = [c.name for c in verdict.criteria]
    assert "net positive after all costs (fees, interest, LLM, X, server)" in names
    assert not any("after fees and interest" in n for n in names)
    feed = next(c for c in verdict.criteria if "WebSocket" in c.name)
    assert feed.ok == (cfg.exchange.price_feed == "ws")
    rest = cfg.model_copy(
        update={"exchange": cfg.exchange.model_copy(update={"price_feed": "rest"})}
    )
    with new_session() as s:
        blocked = golive.evaluate(s, rest, when)
    assert not blocked.ready
    assert not next(c for c in blocked.criteria if "WebSocket" in c.name).ok
    assert golive.MIN_SHADOW_DAYS == 28 and golive.MIN_CLOSED_TRADES == 100
    assert D("1.3") == golive.MIN_PROFIT_FACTOR and D("0.10") == golive.MAX_DRAWDOWN


async def test_pilot_trades_are_never_queued_for_x() -> None:
    from app.db.models import Position
    from app.social import poster

    for mode, track in (("pilot", "pilot"), ("paper", "max")):
        pos = Position(mode=mode, track=track, asset="BTC", symbol="BTCUSDT", direction="long")
        with pytest.raises(ValueError, match="pilot trades never are"):
            await poster.enqueue(None, None, pos, "open", reason=None, style="", dry_run=True)  # type: ignore[arg-type]


def test_websocket_feed_folds_snapshots_and_deltas_into_quotes() -> None:
    import json

    from app.execution.ws_feed import QuoteFeed

    t0 = datetime(2026, 10, 9, 12, tzinfo=UTC)
    feed = QuoteFeed(["BTCUSDT"])

    def msg(kind: str, bids: list[list[str]], asks: list[list[str]]) -> str:
        data = {"s": "BTCUSDT", "b": bids, "a": asks}
        return json.dumps({"topic": "orderbook.1.BTCUSDT", "type": kind, "data": data})

    assert feed.apply(msg("snapshot", [["100.0", "2"]], [["100.1", "3"]]), t0) == "BTCUSDT"
    assert (feed.quotes["BTCUSDT"].bid, feed.quotes["BTCUSDT"].ask) == (D("100.0"), D("100.1"))
    # A delta that only moves the ask keeps the bid.
    feed.apply(msg("delta", [], [["100.1", "0"], ["100.3", "1"]]), t0 + timedelta(seconds=1))
    assert (feed.quotes["BTCUSDT"].bid, feed.quotes["BTCUSDT"].ask) == (D("100.0"), D("100.3"))
    # Control frames, other topics and crossed books change nothing.
    assert feed.apply(json.dumps({"op": "pong"}), t0) is None
    assert feed.apply("not json", t0) is None
    assert feed.apply(msg("snapshot", [["101", "1"]], [["100", "1"]]), t0) is None
    assert feed.quotes["BTCUSDT"].ask == D("100.3")
    # A quote the stream stopped refreshing is not served: the executor falls back to REST.
    assert feed.fresh("BTCUSDT", t0 + timedelta(seconds=5)) is not None
    assert feed.fresh("BTCUSDT", t0 + timedelta(seconds=30)) is None
    assert feed.fresh("ETHUSDT", t0) is None


def test_cost_report_renders() -> None:
    from app.ops import cost_report

    text = cost_report(datetime(2026, 10, 9, tzinfo=UTC))
    assert (
        text.startswith("# Running costs") and "Server (DigitalOcean)" in text and "Total" in text
    )
