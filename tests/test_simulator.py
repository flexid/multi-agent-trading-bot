from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from app.execution import simulator as sim
from app.execution.simulator import ExitReason, Quote, Side

T0 = datetime(2026, 10, 9, 12, tzinfo=UTC)
FEE = D("0.001")


def q(bid: str, ask: str, hours: float = 0) -> Quote:
    return Quote(D(bid), D(ask), T0 + timedelta(hours=hours))


def long_pos(leverage: str = "2") -> sim.PaperPosition:
    return sim.open_position(
        Side.LONG, D(1000), D(leverage), q("99.9", "100"), D(98), D(106), 24, FEE, D("0.001")
    )


def test_open_fills_at_touch_with_fee_and_borrow() -> None:
    p = long_pos("2")
    assert p.entry == D(100) and p.qty == D(10)
    assert p.margin == D(500) and p.borrowed == D(500)
    assert p.fees == D(1)
    s = sim.open_position(
        Side.SHORT, D(1000), D(1), q("99.9", "100"), D(102), D(94), 24, FEE, D("0.001")
    )
    assert s.entry == D("99.9") and s.borrowed == s.qty


def test_liquidation_price_arithmetic() -> None:
    p = long_pos("5")  # borrowed 800 × 1.03 / (0.98 × 10) = 84.08
    assert p.liquidation_price == pytest.approx(D("84.08"), abs=D("0.01"))
    s = sim.open_position(
        Side.SHORT, D(1000), D(4), q("99.9", "100"), D(102), D(94), 24, FEE, D("0.001")
    )
    assert s.liquidation_price == pytest.approx(D("99.9") * D("1.25") / D("1.03"), abs=D("0.01"))
    assert long_pos("1").liquidation_price == 0


def test_interest_accrues_hourly_on_the_borrowed_part() -> None:
    p = long_pos("2")
    later = sim.accrue_interest(p, T0 + timedelta(hours=2, minutes=59), D("0.00001"), D(100))
    assert later.interest == D(500) * D("0.00001") * 2
    assert later.last_interest_at == T0 + timedelta(hours=2)
    s = sim.open_position(
        Side.SHORT, D(1000), D(1), q("99.9", "100"), D(102), D(94), 24, FEE, D("0.001")
    )
    s2 = sim.accrue_interest(s, T0 + timedelta(hours=1), D("0.00001"), D(100))
    assert s2.interest == s.qty * D(100) * D("0.00001")


def test_exit_rules_in_priority_order() -> None:
    p = long_pos("2")
    assert sim.exit_reason(p, q("97.9", "98"), T0) is ExitReason.STOP
    assert sim.exit_reason(p, q("106", "106.1"), T0) is ExitReason.TARGET
    assert sim.exit_reason(p, q("100", "100.1"), T0) is None
    assert sim.exit_reason(p, q("100", "100.1"), T0 + timedelta(hours=24)) is ExitReason.TIME
    assert sim.exit_reason(p, q("100", "100.1"), T0, kill=True) is ExitReason.KILL
    p5 = long_pos("5")
    assert sim.exit_reason(p5, q("84", "84.1"), T0) is ExitReason.LIQUIDATION
    assert sim.exit_reason(p5, q("84.9", "85"), T0) is ExitReason.STOP  # above 84.08


def test_trailing_stop_arms_after_one_r_and_only_tightens() -> None:
    p = long_pos("2")  # R = 2
    p = sim.update_trail(p, q("101", "101.1"))
    assert p.trail_stop is None
    p = sim.update_trail(p, q("103", "103.1"))  # +1.5R → trail at 103 − 2 = 101
    assert p.trail_stop == D(101)
    p = sim.update_trail(p, q("102", "102.1"))  # pullback: trail stays
    assert p.trail_stop == D(101)
    assert sim.exit_reason(p, q("100.9", "101"), T0) is ExitReason.TRAIL
    s = sim.open_position(
        Side.SHORT, D(1000), D(2), q("99.9", "100"), D("101.9"), D(94), 24, FEE, D("0.001")
    )
    s = sim.update_trail(s, q("96", "96.1"))  # R = 2: entry 99.9 − 96.1 = 3.8 ≥ 2 → trail 96.1 + 2
    assert s.trail_stop == D("98.1")


def test_close_pnl_on_price_and_margin() -> None:
    p = long_pos("2")
    fill = sim.close_position(p, q("104", "104.1"), FEE, ExitReason.TARGET)
    assert fill.gross_pnl == D(40)
    assert fill.net_pnl == D(40) - D("1.04") - D(1)
    assert fill.pnl_price_pct == D("0.04")
    assert fill.pnl_margin_pct == fill.net_pnl / D(500)
    liq = sim.close_position(long_pos("5"), q("84", "84.1"), FEE, ExitReason.LIQUIDATION)
    assert liq.net_pnl == -D(200)


def test_below_one_lot_is_rejected() -> None:
    with pytest.raises(ValueError):
        sim.open_position(Side.LONG, D("0.5"), D(1), q("99.9", "100"), D(98), D(106), 24, FEE, D(1))
