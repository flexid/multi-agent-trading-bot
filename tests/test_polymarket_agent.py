from collections.abc import Iterator

import pytest
from sqlalchemy import delete

from app.agents.polymarket import LadderPoint, score_ladder
from app.agents.polymarket_map import Asset, Direction, Kind, MarketMapping, _sane
from tests.test_data_layer import needs_db


@pytest.fixture
def db_clean() -> Iterator[None]:
    from app.db.models import PolymarketMarket, PolymarketPrice
    from app.db.session import new_session

    def wipe() -> None:
        with new_session() as s:
            s.execute(delete(PolymarketPrice))
            s.execute(delete(PolymarketMarket))
            s.commit()

    wipe()
    yield
    wipe()


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


# --- deterministic parser (owner briefing 2026-10-09) ----------------------------------


@pytest.mark.parametrize(
    ("question", "slug", "asset", "kind", "threshold", "direction"),
    [
        (
            "Will the price of Bitcoin be above $84,000 on October 9?",
            "",
            "BTC",
            "price",
            84000,
            "above",
        ),
        (
            "Will the price of Ethereum be less than $2,300 on October 9?",
            "",
            "ETH",
            "price",
            2300,
            "below",
        ),
        (
            "Will the price of Solana be greater than $170 on October 11?",
            "",
            "SOL",
            "price",
            170,
            "above",
        ),
        ("Will Bitcoin reach $90k in October?", "", "BTC", "price", 90000, "above"),
        ("Will Ethereum hit $3,000 by October 31?", "", "ETH", "price", 3000, "above"),
        ("Will Solana dip to $100 October 5-11?", "", "SOL", "price", 100, "below"),
        ("Will BNB dip to $600 by October 31?", "", "BNB", "price", 600, "below"),
        ("Will S&P 500 (SPX) close at >$7,000 in December?", "", "SP500", "price", 7000, "above"),
        ("Will S&P 500 (SPX) close at <$6,000 in December?", "", "SP500", "price", 6000, "below"),
    ],
)
def test_parser_reads_threshold_questions(
    question: str, slug: str, asset: str, kind: str, threshold: float, direction: str
) -> None:
    from app.agents.polymarket_parse import parse

    p = parse(question, slug)
    assert p is not None
    assert (p.asset, p.kind, p.threshold, p.direction) == (asset, kind, threshold, direction)
    assert p.mapping()["source"] == "parser"


def test_parser_reads_ranges_updown_windows_and_leaves_the_rest_to_the_model() -> None:
    from app.agents.polymarket_parse import parse, short_updown

    r = parse("Will the price of Bitcoin be between $82,000 and $84,000 on October 9?")
    assert r is not None and (r.kind, r.low, r.high) == ("range", 82000, 84000)
    sp = parse("Will S&P 500 (SPX) close at $6,000-$6,100 in December?")
    assert sp is not None and (sp.asset, sp.kind, sp.low, sp.high) == ("SP500", "range", 6000, 6100)

    hourly = parse(
        "Bitcoin Up or Down - October 9, 4AM ET", "bitcoin-up-or-down-october-9-2026-4am-et"
    )
    four_h = parse("Bitcoin Up or Down - October 9, 8:00AM-12:00PM ET", "btc-updown-4h-1791547200")
    daily = parse("Solana Up or Down on October 9?", "solana-up-or-down-on-october-9-2026")
    assert hourly is not None and (hourly.asset, hourly.kind, hourly.window_min) == (
        "BTC",
        "updown",
        60,
    )
    assert four_h is not None and four_h.window_min == 240
    assert daily is not None and daily.window_min == 1440
    assert short_updown(
        "Bitcoin Up or Down - October 9, 12:05AM-12:10AM ET", "btc-updown-5m-1791518700"
    )
    assert short_updown(
        "Ethereum Up or Down - October 9, 12:00AM-12:15AM ET", "eth-updown-15m-1791518400"
    )
    assert not short_updown(
        "Bitcoin Up or Down - October 9, 4AM ET", "bitcoin-up-or-down-october-9-2026-4am-et"
    )

    # No asset of ours: "other" without a model call. Ours but unreadable: the model.
    other = parse("Will the Fed cut rates in October?")
    assert other is not None and (other.asset, other.kind) == (None, "other")
    assert parse("Will MicroStrategy announce holding 1M+ BTC by December 31?") is None
    assert parse("Will the Ethereum Volatility Index hit 2500 by October 31?") is None
    assert parse("Will Bitcoin outperform Gold in 2026?") is None


def test_updown_momentum_and_component_weights() -> None:
    from app.agents.polymarket import UpDownPoint, combine, score_updown

    bull = [UpDownPoint("a", 0.65, 240, 2.0, 10_000), UpDownPoint("b", 0.65, 60, 0.5, 1_000)]
    score, evidence = score_updown(bull)
    assert score == pytest.approx(1.0) and evidence and "P(up) 65% over 4 h" in evidence[0]
    assert score_updown([UpDownPoint("a", 0.35, 1440, 20.0, 5_000)])[0] == pytest.approx(-1.0)
    assert score_updown([]) == (0.0, [])
    # shift dominates; the components present share the weight
    assert combine(level=0.0, shift=1.0, updown=0.0) == pytest.approx(0.5)
    assert combine(level=1.0, shift=None, updown=None) == pytest.approx(1.0)
    assert combine(level=0.0, shift=None, updown=1.0) == pytest.approx(0.5)


@needs_db
def test_coverage_tells_no_markets_from_none_usable(db_clean: None) -> None:
    from datetime import UTC, datetime, timedelta

    from app.agents.polymarket import coverage, evaluate
    from app.db.models import PolymarketMarket, PolymarketPrice
    from app.db.session import new_session

    now = datetime(2026, 10, 9, 12, tzinfo=UTC)

    def market(
        id_: str, asset: str, mapping: dict, end_in_hours: float, outcomes: list[str]
    ) -> PolymarketMarket:
        return PolymarketMarket(
            id=id_,
            condition_id="c" + id_,
            slug=id_,
            question="q",
            outcomes=outcomes,
            token_ids=["t1", "t2"],
            end_date=now + timedelta(hours=end_in_hours),
            active=True,
            closed=False,
            asset=asset,
            mapping=mapping,
            mapped_at=now,
            first_seen_at=now,
            updated_at=now,
        )

    with new_session() as s:
        s.add_all(
            [
                market(
                    "p1",
                    "BTC",
                    {"kind": "price", "threshold": 90000, "direction": "above"},
                    48,
                    ["Yes", "No"],
                ),
                market(
                    "p2",
                    "BTC",
                    {"kind": "price", "threshold": 80000, "direction": "below"},
                    48,
                    ["Yes", "No"],
                ),
                market("u1", "BTC", {"kind": "updown", "window_min": 240}, 2, ["Up", "Down"]),
                market(
                    "thin",
                    "SOL",
                    {"kind": "price", "threshold": 100, "direction": "above"},
                    48,
                    ["Yes", "No"],
                ),
                market(
                    "far",
                    "SOL",
                    {"kind": "price", "threshold": 200, "direction": "above"},
                    24 * 90,
                    ["Yes", "No"],
                ),
            ]
        )
        s.add_all(
            [
                PolymarketPrice(
                    market_id="p1", ts=now, prices=["0.3", "0.7"], volume_24h=20000, liquidity=None
                ),
                PolymarketPrice(
                    market_id="p2", ts=now, prices=["0.2", "0.8"], volume_24h=20000, liquidity=None
                ),
                PolymarketPrice(
                    market_id="u1", ts=now, prices=["0.6", "0.4"], volume_24h=30000, liquidity=None
                ),
                PolymarketPrice(
                    market_id="thin", ts=now, prices=["0.5", "0.5"], volume_24h=200, liquidity=None
                ),
                PolymarketPrice(
                    market_id="far", ts=now, prices=["0.5", "0.5"], volume_24h=9000, liquidity=None
                ),
            ]
        )
        s.commit()
        btc, sol, bnb = coverage(s, "BTC", now), coverage(s, "SOL", now), coverage(s, "BNB", now)
        out_btc, out_bnb, out_sol = (evaluate(s, a, 85000.0, 1, now) for a in ("BTC", "BNB", "SOL"))
    assert (btc.open_markets, btc.ladder, btc.updown) == (3, 2, 1) and btc.status.startswith("ok")
    assert bnb.status == "no markets"
    assert sol.usable == 0 and "none usable" in sol.status
    assert (
        sol.dropped["24h volume under $1,000"] == 1 and sol.dropped["resolves beyond 60 days"] == 1
    )
    assert out_btc.score > 0 and out_btc.confidence > 0 and "1 up/down" in out_btc.evidence[0]
    assert out_bnb.risk_flags == ["no polymarket coverage"]
    assert (
        out_sol.risk_flags == ["polymarket: none usable"] and "none usable" in out_sol.evidence[0]
    )
