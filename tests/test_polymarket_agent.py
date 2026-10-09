import pytest

from app.agents.polymarket import LadderPoint, score_ladder
from app.agents.polymarket_map import Asset, Direction, Kind, MarketMapping, _sane


def point(
    threshold: float, p_above: float, prev: float | None = None, vol: float = 10_000
) -> LadderPoint:
    return LadderPoint("m", threshold, p_above, prev, vol, hours_to_resolution=48)


def test_bullish_ladder_scores_positive_and_bearish_negative() -> None:
    spot = 100.0
    bullish = [point(110, 0.8), point(120, 0.6), point(90, 0.99)]
    bearish = [point(110, 0.1), point(120, 0.02), point(90, 0.6)]
    s_bull, level_bull, _, _ = score_ladder(bullish, spot)
    s_bear, level_bear, _, _ = score_ladder(bearish, spot)
    assert s_bull > 0 > s_bear
    assert -1 <= level_bull <= 1 and -1 <= level_bear <= 1


def test_shift_dominates_level_and_is_scaled_to_ten_points() -> None:
    spot = 100.0
    flat_level = [point(110, 0.5, prev=0.4), point(90, 0.5, prev=0.4)]
    score, level, shift, _ = score_ladder(flat_level, spot)
    assert shift == pytest.approx(1.0)  # +10 pp in 24h saturates the fast component
    assert score > level


def test_empty_ladder_is_neutral() -> None:
    assert score_ladder([], 100.0) == (0.0, 0.0, 0.0, [])


def test_mapping_sanity_rules() -> None:
    ok = MarketMapping(
        id="1", asset=Asset.BTC, kind=Kind.PRICE, threshold=84000, direction=Direction.ABOVE
    )
    assert _sane(ok)
    assert not _sane(
        MarketMapping(id="2", asset=Asset.BTC, kind=Kind.PRICE, threshold=None, direction=None)
    )
    assert not _sane(
        MarketMapping(
            id="3", asset=Asset.OTHER, kind=Kind.PRICE, threshold=5, direction=Direction.BELOW
        )
    )
    assert _sane(MarketMapping(id="4", asset=Asset.OTHER, kind=Kind.OTHER))
    with pytest.raises(ValueError):
        MarketMapping(
            id="5", asset=Asset.ETH, kind=Kind.PRICE, threshold=-1, direction=Direction.ABOVE
        )
