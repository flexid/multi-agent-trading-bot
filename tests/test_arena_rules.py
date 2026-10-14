"""The arena's fixed rules (bot spec §1): fee maths, clamping, refusals, bench and round."""

from decimal import Decimal as D

import pytest

from app.arena import rules as R
from app.arena.rulebook import Rulebook, compile_questions

RULES = R.Rules()


def test_fee_maths_matches_the_spec_table() -> None:
    # 10x, T=2%: 0.31% BTC move; 1x, T=2%: 2.11%
    assert R.round_trip_fee_pct(RULES, 10) == D("1.10")
    assert R.price_move_pct(RULES, D(2), 10).quantize(D("0.01")) == D("0.31")
    assert R.price_move_pct(RULES, D(2), 1).quantize(D("0.01")) == D("2.11")


def test_plan_clamps_the_stop_and_refuses_impossible_pairs() -> None:
    p = R.plan_for(RULES, 7, 2, 5.0, 0.7)
    assert p.sl_net_roe_pct == D(2) and "clamped" in p.notes[0]
    assert R.plan_for(RULES, 3, 4, 3.0, 0.65).sl_net_roe_pct == D(3)
    with pytest.raises(R.RulesError):
        R.plan_for(RULES, 11, 5, 5.0, 0.7)  # leverage
    with pytest.raises(R.RulesError):
        R.plan_for(RULES, 5, 1, 1.0, 0.7)  # target below 2%
    with pytest.raises(R.RulesError):
        R.plan_for(RULES, 10, 2, 1.0, 0.7)  # a 1% stop sits inside the 1.1% fee band at 10x
    # the fee rule: fees may eat at most half the target. 2% at 10x costs 1.1%: refused
    with pytest.raises(R.RulesError, match="exceed half"):
        R.plan_for(RULES, 10, 2, 2.0, 0.7)
    assert R.plan_for(RULES, 10, 3, 3.0, 0.7).leverage == 10  # 1.1% of 3% is fine
    assert R.plan_for(RULES, 5, 2, 2.0, 0.3).min_conviction == 0.5  # raised to the floor


def test_take_and_stop_prices_net_the_targets_after_fees() -> None:
    plan = R.plan_for(RULES, 5, 2, 2.0, 0.7)
    entry, tick = D("60000"), D("0.1")
    tp = R.tp_price(RULES, plan, entry, "long", tick)
    sl = R.sl_price(RULES, plan, entry, "long", tick)
    # gross needed = 2 + 0.55 = 2.55% on margin = 0.51% price
    assert tp == D("60306.0")
    # stop: net −2% = gross −(2 − 0.55) = −1.45% on margin = −0.29% price
    assert sl == D("59826.0")
    assert R.tp_price(RULES, plan, entry, "short", tick) == D("59694.0")
    assert R.sl_price(RULES, plan, entry, "short", tick) == D("60174.0")
    # the round trip at the take nets at least the target on margin
    gross = (tp / entry - 1) * 100 * plan.leverage
    assert gross - R.round_trip_fee_pct(RULES, plan.leverage) >= plan.tp_net_roe_pct


def test_loss_bench_and_round() -> None:
    assert R.is_loss(D("0")) and R.is_loss(D("-0.3")) and not R.is_loss(D("0.1"))
    assert not R.is_benched(1, RULES) and R.is_benched(2, RULES)
    assert not R.round_over({"a": True, "b": False, "c": True})
    assert R.round_over({"a": True, "b": True, "c": True})
    assert not R.round_over({})
    # the time limit (owner 2026-10-14): a round also ends after max_hours, not before
    assert not R.round_over({"a": False}, hours_running=47.9, max_hours=48)
    assert R.round_over({"a": False}, hours_running=48, max_hours=48)
    assert not R.round_over({"a": False}, hours_running=500, max_hours=0)
    assert R.qty_for(D(100), 5, D(60000), D("0.001"), D("0.001")) == D("0.008")
    assert R.qty_for(D(10), 1, D(60000), D("0.001"), D("0.001")) == D(0)  # below the minimum
    assert R.net_roe_pct(D("2.10"), D(100)) == D("2.10")


def test_rulebook_compiles_to_jev_questions() -> None:
    rb = Rulebook(
        name="Breakout scalper",
        thesis="Momentum on the 5m.",
        leverage=7,
        tp_net_roe_pct=2,
        sl_net_roe_pct=2,
        min_conviction=0.7,
        entry_long="close above the 12-candle high",
        entry_short="close below the 12-candle low",
        no_trade="range under 0.3%",
    )
    q = compile_questions(rb)
    assert set(q) == {"signal", "conviction"}
    assert compile_questions(rb, side="long") == {}  # no early exit written
    rb2 = rb.model_copy(update={"early_exit": "price back at the 1h average"})
    assert set(compile_questions(rb2, side="short")) == {"early_exit"}
    assert rb.public()["tp_net_roe_pct"] == 2
    with pytest.raises(ValueError):
        Rulebook.model_validate({**rb.model_dump(), "leverage": 12})


def test_jev_state_is_compact() -> None:
    """Jev's window is small: at most 328 candles, prices to one decimal, integer volumes,
    times as minutes before now; the whole state well under 20k characters."""
    import asyncio
    import json
    from datetime import UTC, datetime, timedelta

    from app.arena.candles import JEV_COUNT, CandleBuffer

    now = datetime(2026, 10, 14, tzinfo=UTC)

    class Src:
        async def klines(self, interval: str, limit: int = 151) -> list[list[float]]:
            step = int(interval)
            return [
                [
                    (now - timedelta(minutes=(limit - i) * step)).timestamp() * 1000,
                    60000.123,
                    60010.456,
                    59990.789,
                    60005.0001,
                    1234.5678,
                ]
                for i in range(limit)
            ]

        async def last_price(self) -> D:
            return D(60000)

    buf = CandleBuffer(Src())  # type: ignore[arg-type]
    asyncio.run(buf.refresh())
    state = buf.state(None, now)
    assert {k: len(v) for k, v in state["candles"].items()} == JEV_COUNT
    last = state["candles"]["1m"][-1]
    assert last == [1, 60000.1, 60010.5, 59990.8, 60005.0, 1234]
    assert len(json.dumps(state, separators=(",", ":"))) < 20000


def test_the_gate_is_jevs_entry_probability_and_the_score_is_kept() -> None:
    """Probe of 2026-10-14: on a textbook setup Jev gives the entry about 0.7 and the
    rubric score about 0.5. The gate uses the probability; the score rides along."""
    import asyncio

    from app.arena import reflex

    class Fake:
        answers = {
            "signal": {
                "choice": "long",
                "probabilities": {"long": 0.69, "short": 0.01, "none": 0.30},
            },
            "conviction": {"score": 2.0},  # level 2 of 4 → 0.5
        }

    async def fake_judge(*a: object, **k: object) -> Fake:
        return Fake()

    orig = reflex.judge  # type: ignore[attr-defined]
    reflex.judge = fake_judge  # type: ignore[attr-defined,assignment]
    try:
        rb = Rulebook(
            name="x",
            thesis="t",
            leverage=5,
            tp_net_roe_pct=3,
            sl_net_roe_pct=3,
            min_conviction=0.6,
            entry_long="a",
            entry_short="b",
            no_trade="c",
        )
        sig = asyncio.run(reflex.decide_entry(rb, {"candles": {}}, model="jev-test", trader="t"))
    finally:
        reflex.judge = orig  # type: ignore[attr-defined]
    assert sig is not None and sig.choice == "long"
    assert sig.conviction == 0.69 and sig.probabilities["score"] == 0.5


def test_the_bounce_since_a_breakout_counts_only_candles_after_it_closed() -> None:
    """2026-10-14: the window included the breakout candle's own minutes, so a 1.2%
    'bounce' appeared at the moment of a breakdown. Only 1m candles after the 15m candle
    closed count, and none at all right at its close."""
    from app.arena.candles import breakout_facts

    base = 83000.0
    t15 = 1_760_400_000_000
    f15 = [[t15 + i * 900_000, base, base + 50, base - 50, base, 100.0] for i in range(40)]
    bo_open = t15 + 40 * 900_000
    f15.append([bo_open, base - 40, base - 30, base - 600, base - 550, 260.0])  # a breakdown
    bo_close = base - 550
    # 1m candles: the breakout's own 15 minutes (prices still high), then 4 after it closed
    f1 = [
        [bo_open + i * 60_000, base - 100, base - 50, base - 600, base - 400, 10.0]
        for i in range(15)
    ]
    f1 += [
        [
            bo_open + 900_000 + i * 60_000,
            bo_close,
            bo_close * 1.002,
            bo_close * 0.999,
            bo_close * 1.0015,
            10.0,
        ]
        for i in range(4)
    ]
    facts = breakout_facts({"15m": f15, "1m": f1[:15]})["last_15m_breakout"]
    assert facts["direction"] == "down" and facts["minutes_since_close"] == 0
    assert facts["deepest_bounce_pct"] == 0.0
    facts = breakout_facts({"15m": f15, "1m": f1})["last_15m_breakout"]
    assert facts["minutes_since_close"] == 4
    assert abs(facts["deepest_bounce_pct"] - 0.2) < 0.01  # the 0.2% high after the close, not 0.6%
