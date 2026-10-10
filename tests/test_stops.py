from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from app.decision.pm import Direction
from app.execution import simulator as sim
from app.execution.simulator import ExitReason, Quote, Side
from app.risk.stops import next_buffer, place_stop, round_levels, swing_levels, wick_out_for

T0 = datetime(2026, 10, 10, 12, tzinfo=UTC)


def q(bid: str, ask: str, minutes: int = 0) -> Quote:
    return Quote(D(bid), D(ask), T0 + timedelta(minutes=minutes))


def test_round_levels_follow_a_one_two_five_grid_near_the_price() -> None:
    lv = round_levels(D("82500"), D("78000"), D("87000"))
    assert lv == [
        D("78000"),
        D("79000"),
        D("80000"),
        D("81000"),
        D("82000"),
        D("83000"),
        D("84000"),
        D("85000"),
        D("86000"),
        D("87000"),
    ]
    assert round_levels(D("0.37"), D("0.34"), D("0.40"))[0] == D("0.34")  # 0.005 grid


def test_swing_levels_are_pivots() -> None:
    highs = [D(x) for x in (10, 11, 12, 11, 10, 11, 13, 12, 11)]
    lows = [D(x) for x in (9, 10, 11, 10, 8, 9, 10, 9, 8)]
    lv = swing_levels(highs, lows)
    assert D(12) in lv and D(13) in lv and D(8) in lv


def test_stop_moves_beyond_a_level_with_the_buffer_and_the_hard_stop_sits_an_atr_further() -> None:
    atr = D("1000")
    # long from 82,500 with a stop at 80,100: 80,000 is a round number within 0.5 ATR
    placed = place_stop(Direction.LONG, D("82500"), D("80100"), atr, [D("80000")])
    assert placed.moved and placed.soft == D("79500") and placed.hard == D("78500")
    # short: the mirror image
    placed = place_stop(Direction.SHORT, D("82500"), D("84900"), atr, [D("85000")])
    assert placed.soft == D("85500") and placed.hard == D("86500")
    # clear of levels: untouched, hard stop still an ATR beyond
    placed = place_stop(Direction.LONG, D("82500"), D("80100"), atr, [D("70000")])
    assert not placed.moved and placed.soft == D("80100") and placed.hard == D("79100")
    # never further than 3 ATR from the entry
    placed = place_stop(Direction.LONG, D("82500"), D("79600"), atr, [D("79500"), D("79000")])
    assert placed.soft >= D("82500") - 3 * atr
    # no ATR: nothing changes, hard = soft (legacy touch behaviour)
    placed = place_stop(Direction.LONG, D("100"), D("98"), None, [D("98")])
    assert placed.soft == placed.hard == D("98") and not placed.moved


def test_soft_stop_needs_a_fifteen_minute_close_and_the_hard_stop_a_touch() -> None:
    p = sim.open_position(
        Side.LONG,
        D(1000),
        D(1),
        q("100", "100.1"),
        D(97),
        D(110),
        24,
        D("0.001"),
        D("0.001"),
        hard_stop=D(96),
    )
    assert (
        sim.exit_reason(p, q("96.5", "96.6", 1), T0 + timedelta(minutes=1)) is None
    )  # touched soft only
    assert (
        sim.exit_reason(p, q("96.5", "96.6", 1), T0 + timedelta(minutes=1), close_15m=D("97.2"))
        is None
    )
    assert (
        sim.exit_reason(p, q("96.5", "96.6", 1), T0 + timedelta(minutes=1), close_15m=D("96.8"))
        is ExitReason.STOP
    )
    assert (
        sim.exit_reason(p, q("95.9", "96.0", 1), T0 + timedelta(minutes=1)) is ExitReason.STOP_HARD
    )
    assert p.stop_distance == D("4.1")  # sizing distance is the hard stop's (entry at the ask)
    legacy = sim.open_position(
        Side.LONG, D(1000), D(1), q("100", "100.1"), D(97), D(110), 24, D("0.001"), D("0.001")
    )
    assert (
        sim.exit_reason(legacy, q("96.9", "97.0", 1), T0 + timedelta(minutes=1)) is ExitReason.STOP
    )


def test_wick_out_and_buffer_tuning() -> None:
    assert wick_out_for("long", D(110), [D(105), D(111)], [D(100), D(104)])
    assert not wick_out_for("short", D(90), [D(105), D(101)], [D(95), D(92)])
    assert next_buffer(D("0.5"), 0.5, 10) == D("0.5")  # too few stops to act
    assert next_buffer(D("0.5"), 0.5, 25) == D("0.75")
    assert next_buffer(D("0.5"), 0.1, 25) == D("0.25")
    assert next_buffer(D("1.5"), 0.9, 25) == D("1.5") and next_buffer(D("0.25"), 0.0, 25) == D(
        "0.25"
    )
