from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.agents import x_sentiment as xs
from app.db.models import XPostRecord

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


def post(
    i: int,
    stance: str,
    kind: str = "analysis",
    cred: float = 0.8,
    shock: bool = False,
    age_h: float = 2,
) -> XPostRecord:
    return XPostRecord(
        id=str(i),
        text="x",
        fetched_at=NOW,
        created_at=NOW - timedelta(hours=age_h),
        asset="BTC",
        stance=stance,
        kind=kind,
        credibility=Decimal(str(cred)),
        shock=shock,
        labeled_at=NOW,
    )


def test_credible_bullish_posts_score_positive() -> None:
    rows = [post(i, "bullish") for i in range(6)] + [post(10, "bearish", cred=0.3)]
    out = xs.aggregate(rows, "BTC", 1, NOW)
    assert out.score > 0.5 and out.confidence > 0.3
    assert out.valid


def test_shill_barely_moves_the_level_and_few_posts_are_shrunk() -> None:
    rows = [post(1, "bearish", kind="shill", cred=0.2)]
    out = xs.aggregate(rows, "SOL", 1, NOW)
    assert -0.1 < out.score < 0
    assert out.confidence < 0.15


def test_one_sided_crowd_is_halved_and_flagged() -> None:
    rows = [post(i, "bullish", cred=0.7) for i in range(10)]
    out = xs.aggregate(rows, "BTC", 1, NOW)
    balanced = xs.aggregate(rows[:4] + [post(99, "bearish", cred=0.7)], "BTC", 1, NOW)
    assert any("one-sided" in f for f in out.risk_flags)
    assert out.score < 0.6  # halved from near +1
    assert not any("one-sided" in f for f in balanced.risk_flags)


def test_news_shock_flag_needs_credibility_and_recency() -> None:
    fresh = xs.aggregate([post(1, "bearish", kind="news", cred=0.9, shock=True)], "BTC", 1, NOW)
    stale = xs.aggregate(
        [post(2, "bearish", kind="news", cred=0.9, shock=True, age_h=10)], "BTC", 1, NOW
    )
    weak = xs.aggregate([post(3, "bearish", kind="news", cred=0.3, shock=True)], "BTC", 1, NOW)
    assert any("news shock" in f for f in fresh.risk_flags)
    assert not any("news shock" in f for f in stale.risk_flags)
    assert not any("news shock" in f for f in weak.risk_flags)


def test_spx6900_gains_confidence_faster() -> None:
    rows_btc = [post(i, "bullish") for i in range(5)]
    rows_spx = [post(i, "bullish") for i in range(5)]
    assert (
        xs.aggregate(rows_spx, "SPX6900", 1, NOW).confidence
        > xs.aggregate(rows_btc, "BTC", 1, NOW).confidence
    )


def test_no_posts_is_neutral_with_zero_confidence() -> None:
    out = xs.aggregate([], "BNB", 1, NOW)
    assert out.score == 0 and out.confidence == 0


def test_mention_volume_scales_confidence_and_flags_spikes_spx6900_twice_as_much() -> None:
    rows = [post(i, "bullish") for i in range(10)]
    plain = xs.aggregate(rows, "SPX6900", 1, NOW)
    quiet = xs.aggregate(rows, "SPX6900", 1, NOW, mentions=(50, 100.0))
    loud = xs.aggregate(rows, "SPX6900", 1, NOW, mentions=(260, 100.0))
    assert quiet.confidence < plain.confidence < loud.confidence
    assert loud.score == plain.score  # attention never changes the direction
    assert any("attention spike" in f for f in loud.risk_flags)
    assert any("attention fading" in f for f in quiet.risk_flags)
    assert any(e.startswith("mentions: 260 in 24h") for e in loud.evidence)
    btc_loud = xs.aggregate(
        [post(i, "bullish") for i in range(10)], "BTC", 1, NOW, mentions=(260, 100.0)
    )
    btc_plain = xs.aggregate([post(i, "bullish") for i in range(10)], "BTC", 1, NOW)
    assert (
        0
        < (btc_loud.confidence / btc_plain.confidence - 1)
        < (loud.confidence / plain.confidence - 1)
    )
    # an empty baseline (series too short) changes nothing
    assert xs.aggregate(rows, "BTC", 1, NOW, mentions=(0, 0.0)).evidence == btc_plain.evidence
