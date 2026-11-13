"""The slow book's signal: three votes, a majority, a volatility-scaled weight."""

from __future__ import annotations

import pytest

from app.strategy import trend


def series(start: float, daily: float, n: int = 80) -> list[float]:
    out = [start]
    for _ in range(n - 1):
        out.append(out[-1] * (1 + daily))
    return out


def test_an_uptrend_votes_long_on_every_lookback() -> None:
    s = trend.signal(series(100, 0.01))
    assert s is not None and s.votes == (1, 1, 1) and s.side == "long"
    assert s.close == pytest.approx(100 * 1.01**79)


def test_a_downtrend_votes_short_and_a_split_follows_the_majority() -> None:
    assert trend.signal(series(100, -0.01)).side == "short"  # type: ignore[union-attr]
    # down for a long while, then up for three weeks: 14 and... the last 20 days up
    closes = series(100, -0.004, 60) + series(78.6, 0.012, 20)
    s = trend.signal(closes)
    assert s is not None and s.votes[0] == 1  # the 14-day vote has turned
    assert s.side == ("long" if sum(s.votes) > 0 else "short")
    assert trend.side_of((1, -1, 0)) == "flat" and trend.side_of((1, 1, -1)) == "long"


def test_the_weight_scales_with_volatility_and_never_exceeds_the_slot() -> None:
    calm = [100 * (1 + 0.002 * ((-1) ** i)) for i in range(80)]  # about 4% a year
    wild = [100 * (1 + 0.05 * ((-1) ** i)) for i in range(80)]  # about 190% a year
    c, w = trend.signal(calm), trend.signal(wild)
    assert c is not None and w is not None
    assert c.weight == 1.0  # calmer than the target: the whole slot, never more
    assert w.vol is not None and w.weight == pytest.approx(0.25 / w.vol)
    assert 0.1 < w.weight < 0.2
    half = trend.signal(wild, vol_target=0.5)
    assert half is not None and half.weight == pytest.approx(2 * w.weight)


def test_too_little_history_gives_no_signal() -> None:
    assert trend.signal(series(100, 0.01, 56)) is None  # the 56-day vote needs 57 closes
    assert trend.signal(series(100, 0.01, 57)) is not None


def test_annual_vol_matches_the_backtest_arithmetic() -> None:
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(7)
    closes = list(100 * np.cumprod(1 + rng.normal(0, 0.03, 120)))
    expect = pd.Series(closes).pct_change().rolling(20).std().iloc[-1] * np.sqrt(365)
    assert trend.annual_vol(closes) == pytest.approx(float(expect))
