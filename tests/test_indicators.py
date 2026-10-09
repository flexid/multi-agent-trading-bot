import numpy as np
import pandas as pd
import pytest

from app.agents import indicators as ind
from app.agents.schema import AgentOutput, Horizon
from app.backtest import backtest_frame, evaluate_scores


def synthetic(n: int = 400, drift: float = 0.002, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = drift + 0.01 * rng.standard_normal(n)
    close = 100 * np.exp(np.cumsum(rets))
    high = close * (1 + 0.004 * rng.random(n))
    low = close * (1 - 0.004 * rng.random(n))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    vol = 1000 + 100 * rng.random(n)
    idx = pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx
    )


def test_uptrend_scores_positive_and_downtrend_negative() -> None:
    up = ind.evaluate("BTC", synthetic(drift=0.003), "240", None, data_age_min=5)
    down = ind.evaluate("BTC", synthetic(drift=-0.003), "240", None, data_age_min=5)
    assert up.score > 0.2
    assert down.score < -0.2
    assert up.valid and up.horizon is Horizon.D1_3
    assert up.agent == "indicators"


def test_scores_are_deterministic_and_bounded() -> None:
    a = ind.evaluate("ETH", synthetic(), "240", None, 1)
    b = ind.evaluate("ETH", synthetic(), "240", None, 1)
    assert a.score == b.score and a.confidence == b.confidence
    assert -1 <= a.score <= 1 and 0 <= a.confidence <= 1


def test_funding_and_oi_rules() -> None:
    df = ind.features(synthetic(), "240")
    df.attrs["interval"] = "240"
    i = len(df) - 1
    crowded = ind.score_row(df, i, ind.PerpInputs(funding_rate=0.0006, oi_change_24h=0.0))
    neutral = ind.score_row(df, i, ind.PerpInputs(funding_rate=0.0001, oi_change_24h=0.0))
    assert crowded["funding"] == pytest.approx(-1.0)
    assert neutral["funding"] == 0.0
    assert ind.combine({"trend": 1.0, "macd": 1.0})[1] == 1.0
    assert ind.combine({"trend": 1.0, "macd": -1.0})[1] == 0.0


def test_too_few_bars_is_rejected() -> None:
    with pytest.raises(ValueError):
        ind.evaluate("SOL", synthetic(n=100), "240", None, 1)


def test_stale_output_is_invalid() -> None:
    out = ind.evaluate("BNB", synthetic(), "240", None, data_age_min=31)
    assert not out.valid
    assert isinstance(out, AgentOutput)


def test_score_series_uses_only_past_bars() -> None:
    """Changing future bars must not change earlier scores."""
    base = synthetic(n=300)
    df1 = ind.features(base, "240")
    altered = base.copy()
    altered.loc[altered.index[-20:], "close"] *= 1.5
    df2 = ind.features(altered, "240")
    s1, s2 = ind.score_series(df1), ind.score_series(df2)
    assert np.allclose(s1.iloc[210:260].to_numpy(), s2.iloc[210:260].to_numpy())


def test_backtest_finds_signal_in_a_trending_series() -> None:
    result = backtest_frame("BTC", synthetic(n=600, drift=0.003), "240")
    assert result.bars > 300
    assert all(s.n > 100 for s in result.stats)
    stats = evaluate_scores(
        pd.Series(np.nan, index=range(10)), pd.Series(1.0, index=range(10)), "240"
    )
    assert all(np.isnan(s.ic) for s in stats)
