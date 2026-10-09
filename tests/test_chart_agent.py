import numpy as np
import pandas as pd

from app.agents import chart_patterns as cp
from app.agents.chart_patterns import Direction, VisionRead
from tests.test_indicators import synthetic


def test_swings_and_levels_cluster() -> None:
    df = synthetic(n=200, drift=0.0)
    sh, sl = cp.swings(df)
    assert sh and sl
    assert all(0 < i < len(df) for i, _ in sh)
    levels = cp.cluster_levels([100.0, 100.2, 100.3, 150.0, 151.0, 200.0])
    assert len(levels) == 1 and 100.0 < levels[0] < 100.4  # 150/151 and 200 are singletons


def test_code_track_detects_an_upside_breakout() -> None:
    df = synthetic(n=200, drift=0.0, seed=3)
    up = df.copy()
    last = up.index[-3:]
    up.loc[last, ["open", "high", "low", "close"]] *= 1.08
    up.loc[last, "volume"] *= 3
    read = cp.code_track(up, "240")
    assert read.breakout is Direction.UP
    assert read.score > 0.4
    assert read.range_position > 0.9


def test_code_track_is_bounded_and_deterministic() -> None:
    df = synthetic(n=200)
    a, b = cp.code_track(df, "240"), cp.code_track(df, "240")
    assert a == b
    assert -1 <= a.score <= 1 and 0 <= a.range_position <= 1


def test_combine_rewards_agreement_and_penalizes_disagreement() -> None:
    code = {"240": cp.CodeRead("240", 0.6, [110.0], [90.0], Direction.UP, False, 0.3, 0.8)}
    agree = {
        "240": VisionRead(
            pattern="breakout", direction=Direction.UP, confidence=0.8, key_levels=[110], note=""
        )
    }
    disagree = {
        "240": VisionRead(
            pattern="double top", direction=Direction.DOWN, confidence=0.8, key_levels=[], note=""
        )
    }
    a = cp.combine("BTC", code, agree, 1)
    d = cp.combine("BTC", code, disagree, 1)
    n = cp.combine("BTC", code, {}, 1)
    assert a.score > n.score > d.score > 0
    assert a.confidence > n.confidence > d.confidence


def test_render_returns_png() -> None:
    img = cp.render(synthetic(n=150), "TEST 4h")
    assert img.media_type == "image/png"
    assert np.frombuffer(
        __import__("base64").b64decode(img.data_b64)[:8], dtype=np.uint8
    ).tolist() == [137, 80, 78, 71, 13, 10, 26, 10]


def test_false_breakout_is_flagged() -> None:
    df = synthetic(n=200, drift=0.0, seed=5)
    fb = df.copy()
    cols = ["open", "high", "low", "close"]
    fb.loc[fb.index[-3], cols] *= 1.08  # poke above the range
    fb.loc[fb.index[-3], "volume"] *= 3
    read = cp.code_track(fb, "240")  # last close is back inside
    assert read.false_breakout
    assert read.breakout is Direction.NONE
    out = cp.combine("ETH", {"240": read}, {}, 1)
    assert "false breakout on 4h" in out.risk_flags


def test_frames_without_enough_bars_are_skipped_in_combine() -> None:
    out = cp.combine("SOL", {}, {}, 5)
    assert out.score == 0 and out.confidence == 0
    assert isinstance(out.evidence, list)
    assert pd.isna(out.score) is False
