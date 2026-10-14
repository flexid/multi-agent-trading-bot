"""Structured conditions evaluated in code: the exact checks Jev could not make."""

from app.arena.conditions import Condition, Frames, Ref, describe, evaluate

# 1m rows: [t, open, high, low, close, volume]; index 0 is the last row
F: Frames = {
    "5m": [[0, 100, 101, 99, 100.5, 10]] * 8 + [[0, 100.5, 102, 100.4, 101.8, 30]],
    "1m": [
        [0, 100, 100.2, 99.9, 100.1, 5],
        [0, 100.1, 100.3, 100, 100.2, 6],
        [0, 100.2, 100.4, 100.1, 100.3, 7],
        [0, 100.3, 101.9, 100.3, 101.8, 12],
    ],
    "1h": [[0, 99, 102, 98, 101, 500]],
}


def test_breakout_and_volume_conditions() -> None:
    above_prior_8 = Condition(
        timeframe="5m", field="close", op=">", ref=Ref(field="high", agg="max", lookback=8, index=1)
    )
    assert evaluate(F, [above_prior_8])["met"]
    green_5m = Condition(timeframe="5m", field="close", op=">", ref=Ref(field="open", index=0))
    prev_green = Condition(
        timeframe="5m", field="close", index=1, op=">", ref=Ref(field="open", index=1)
    )
    assert evaluate(F, [green_5m])["met"] and evaluate(F, [prev_green])["met"]
    vol_top = Condition(
        timeframe="1m", field="volume", op=">", ref=Ref(agg="max", lookback=3, index=1)
    )
    assert evaluate(F, [vol_top])["met"]
    hour_green = Condition(timeframe="1h", field="close", op=">", ref=Ref(field="open", index=0))
    res = evaluate(F, [above_prior_8, green_5m, vol_top, hour_green])
    assert res["met"] and res["count_met"] == 4


def test_percent_scale_and_missing_data() -> None:
    # the last 1m close is 1.3% above the close three candles earlier
    up = Condition(
        timeframe="1m", field="close", op=">", pct=True, ref=Ref(value=1.0, field="close", index=3)
    )
    assert evaluate(F, [up])["met"]
    too_far = Condition(
        timeframe="1m", field="close", op=">", pct=True, ref=Ref(value=2.0, field="close", index=3)
    )
    assert not evaluate(F, [too_far])["met"]
    # 1.5 × the average volume of the prior 8 (10 each): 30 > 15
    big = Condition(
        timeframe="5m", field="volume", op=">", ref=Ref(agg="avg", lookback=8, index=1, scale=1.5)
    )
    assert evaluate(F, [big])["met"]
    # not enough candles: unknown, and unknown never counts as met
    deep = Condition(timeframe="1h", field="close", op=">", ref=Ref(field="close", index=5))
    r = evaluate(F, [deep])
    assert r["results"] == [None] and not r["met"]
    assert (
        describe(
            above := Condition(
                timeframe="5m",
                field="close",
                op=">",
                ref=Ref(field="high", agg="max", lookback=8, index=1),
            )
        )
        == "5m close[0] > max high over 8 5m from [1]"
    )
    assert above.model_dump()["pct"] is False
