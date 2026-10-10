from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from app.decision.consensus import Agreement, Consensus
from app.decision.pm import Direction, Proposal
from app.risk import engine
from app.risk import leverage as lev

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


def inputs(**over: object) -> lev.LeverageInputs:
    base: dict[str, object] = {
        "asset": "BTC",
        "stop_distance": D("0.01"),
        "risk_per_trade": D("0.005"),
        "capital_share": D("0.20"),
        "leverage_max": D(10),
        "leverage_max_spx6900": D(3),
        "ceiling": D(10),
        "atr_extreme": False,
        "event_today": False,
        "risk_off_coupled": False,
        "agreement": Agreement.AGREE,
        "conviction": D("0.7"),
        "two_losing_days": False,
        "half_risk": False,
    }
    base.update(over)
    return lev.LeverageInputs(**base)  # type: ignore[arg-type]


def test_base_formula_matches_spec_examples() -> None:
    assert lev.compute(inputs(stop_distance=D("0.01"))).leverage == D("2.5")
    assert lev.compute(inputs(stop_distance=D("0.005"))).leverage == D("5")
    assert lev.compute(inputs(stop_distance=D("0.001"))).leverage == D(10)  # absolute cap
    assert lev.compute(inputs(stop_distance=D(0))).leverage == D(1)


def test_caps_take_the_lowest() -> None:
    assert lev.compute(inputs(stop_distance=D("0.002"), asset="SPX6900")).leverage == D(3)
    assert lev.compute(inputs(stop_distance=D("0.002"), event_today=True)).leverage == D(2)
    assert lev.compute(inputs(stop_distance=D("0.002"), risk_off_coupled=True)).leverage == D(2)
    assert lev.compute(inputs(stop_distance=D("0.002"), ceiling=D(2))).leverage == D(2)
    assert lev.compute(inputs(stop_distance=D("0.002"), fng_extreme=True)).leverage == D(2)
    r = lev.compute(inputs(stop_distance=D("0.005"), atr_extreme=True))
    assert r.leverage == D("2.5") and ("atr_extreme", D("2.5")) in r.caps
    assert lev.compute(inputs(stop_distance=D("0.005"), two_losing_days=True)).leverage == D("2.5")
    assert lev.compute(inputs(stop_distance=D("0.005"), half_risk=True)).leverage == D("2.5")


def test_disagreement_or_low_conviction_means_1x_no_borrow() -> None:
    r = lev.compute(inputs(conviction=D("0.49")))
    assert r.leverage == D(1) and not r.borrow_allowed
    assert lev.compute(inputs(conviction=D("0.5"))).borrow_allowed


def test_llm_can_only_lower() -> None:
    assert lev.compute(inputs(stop_distance=D("0.005"), llm_cap=D(3))).leverage == D(3)
    assert lev.compute(inputs(stop_distance=D("0.005"), llm_cap=D(20))).leverage == D(5)
    assert lev.compute(inputs(llm_cap=D(0))).leverage == D(1)


def test_liquidation_buffer_three_times_stop() -> None:
    assert lev.liquidation_buffer_ok(D(5), D("0.01"))  # liq 15% away ≥ 3%
    assert not lev.liquidation_buffer_ok(D(10), D("0.02"))  # liq 5% away < 6%
    assert lev.reduce_for_liquidation(D(10), D("0.02")) == D(9)  # first step where 1/L − 5% ≥ 6%
    assert lev.reduce_for_liquidation(D(10), D("0.04")) == D(6)  # needs ≥ 12%: 6x gives 12.4%
    assert lev.liquidation_distance(D(5)) == pytest.approx(D("0.159"), abs=D("0.001"))
    assert lev.liquidation_distance(D(4), short=True) == pytest.approx(D("0.2136"), abs=D("0.001"))
    assert lev.liquidation_distance(D(1)) == D(1)


# --- engine -----------------------------------------------------------------------


def limits(**over: object) -> engine.Limits:
    base: dict[str, object] = {
        "capital_max": D(10000),
        "risk_per_trade": D("0.005"),
        "capital_share": D("0.20"),
        "leverage_max": D(10),
        "leverage_max_spx6900": D(3),
        "gross_exposure_max": D(3),
        "depth_cap": D("0.05"),
        "day_loss_stop": D("-0.02"),
    }
    base.update(over)
    return engine.Limits(**base)  # type: ignore[arg-type]


def account(**over: object) -> engine.AccountState:
    base: dict[str, object] = {
        "equity": D(10000),
        "starting_capital": D(10000),
        "peak_equity": D(10000),
        "day_pnl_pct": D(0),
        "day_high_pnl_pct": D(0),
        "gross_exposure": D(0),
        "trades_today": {},
        "losing_days_in_row": 0,
        "paused_until": None,
        "pause_count_30d": 0,
        "emergency_brake": False,
        "half_risk": False,
        "leverage_ceiling": D(10),
    }
    base.update(over)
    return engine.AccountState(**base)  # type: ignore[arg-type]


def market(**over: object) -> engine.MarketState:
    base: dict[str, object] = {
        "depth_quote_2pct": D(1_000_000),
        "atr_extreme": False,
        "event_today": False,
        "risk_off_coupled": False,
        "fng_extreme": False,
        "taker_fee": D("0.001"),
        "hourly_borrow_rate": D("0.000005"),
        "margin_enabled": True,
        "short_allowed": True,
    }
    base.update(over)
    return engine.MarketState(**base)  # type: ignore[arg-type]


def cons(
    direction: Direction = Direction.LONG,
    conviction: float = 0.7,
    agreement: Agreement = Agreement.AGREE,
    **geo: float,
) -> Consensus:
    if direction is Direction.LONG:
        g = {"entry_low": 99.0, "entry_high": 101.0, "stop": 99.0, "target": 106.0}
    else:
        g = {"entry_low": 99.0, "entry_high": 101.0, "stop": 101.0, "target": 94.0}
    g.update(geo)
    p = Proposal(
        asset="BTC",
        direction=direction,
        max_hold_hours=24,
        score=0.5,
        conviction=conviction,
        reasons=["r"],
        **g,
    )
    return Consensus(direction, agreement, 0.3, 0.4, conviction, 5, "PMs agree", p)


def test_account_rules_order_and_effects() -> None:
    assert engine.account_rules(account(emergency_brake=True), limits(), NOW)[0].effect == "brake"
    assert engine.account_rules(account(equity=D(7400)), limits(), NOW)[0].rule == "emergency_brake"
    assert (
        engine.account_rules(account(pause_count_30d=2), limits(), NOW)[0].rule == "emergency_brake"
    )
    assert engine.account_rules(account(equity=D(8900)), limits(), NOW)[0].effect == "pause"
    paused = account(paused_until=NOW + timedelta(hours=1))
    assert engine.account_rules(paused, limits(), NOW)[0].rule == "paused"
    assert (
        engine.account_rules(account(day_pnl_pct=D("-0.02")), limits(), NOW)[0].effect
        == "close_all"
    )
    lock = engine.account_rules(account(day_high_pnl_pct=D("0.016")), limits(), NOW)
    assert lock and lock[0].rule == "day_profit_lock" and lock[0].effect == "protect"
    assert engine.account_rules(account(), limits(), NOW) == []


def test_assess_sizes_a_clean_long() -> None:
    a = engine.assess(cons(), account(), market(), limits())
    assert a.allowed and a.plan is not None
    plan = a.plan
    assert plan.leverage == D("2.5")  # entry 100, stop 99 → 1% → 0.005 / (0.2 × 0.01)
    assert plan.margin == D(2000) and plan.notional == D(5000)
    assert plan.borrow and plan.direction is Direction.LONG
    assert plan.entry == D(100) and plan.stop == D(99) and plan.target == D(106)


def test_assess_blocks() -> None:
    assert not engine.assess(cons(direction=Direction.FLAT), account(), market(), limits()).allowed
    assert not engine.assess(cons(), account(trades_today={"BTC": 3}), market(), limits()).allowed
    assert not engine.assess(
        cons(), account(), market(), limits(), account_hits=[engine.RuleHit("paused", "", "block")]
    ).allowed
    short_no_margin = engine.assess(
        cons(Direction.SHORT), account(), market(short_allowed=False), limits()
    )
    assert not short_no_margin.allowed and short_no_margin.hits[-1].rule == "short_unavailable"
    short_partial = engine.assess(
        cons(Direction.SHORT, agreement=Agreement.PARTIAL), account(), market(), limits()
    )
    assert not short_partial.allowed and short_partial.hits[-1].rule == "short_needs_borrow"


def test_cost_rule_requires_three_times_fees_plus_interest() -> None:
    tight = engine.assess(cons(target=100.4), account(), market(), limits())  # 0.4% < 3 × 0.2%
    assert not tight.allowed and tight.hits[-1].rule == "cost_rule"
    assert engine.assess(cons(target=100.7), account(), market(), limits()).allowed


def test_depth_and_exposure_caps_shrink_the_position() -> None:
    thin = engine.assess(cons(), account(), market(depth_quote_2pct=D(20_000)), limits())
    assert thin.allowed and thin.plan is not None and thin.plan.notional == D(1000)  # 5% of 20k
    assert any(h.rule == "depth_cap" for h in thin.hits)
    full = engine.assess(cons(), account(gross_exposure=D(29_500)), market(), limits())
    assert full.allowed and full.plan is not None and full.plan.notional == D(500)
    none = engine.assess(cons(), account(gross_exposure=D(30_000)), market(), limits())
    assert not none.allowed and none.hits[-1].rule == "no_room"


def test_capital_max_and_half_risk_bound_the_margin() -> None:
    capped = engine.assess(
        cons(), account(equity=D(50_000)), market(), limits(capital_max=D(10_000))
    )
    assert (
        capped.plan is not None
        and capped.plan.margin == D(2000) * capped.plan.leverage / capped.plan.leverage
    )
    assert capped.plan.notional == D(2000) * capped.plan.leverage
    half = engine.assess(cons(), account(half_risk=True), market(), limits())
    assert half.plan is not None and half.plan.notional < capped.plan.notional


def test_low_conviction_long_trades_at_1x_without_borrow() -> None:
    a = engine.assess(cons(conviction=0.4), account(), market(), limits())
    assert a.allowed and a.plan is not None
    assert a.plan.leverage == D("1.0") and not a.plan.borrow


def test_agreed_short_at_1x_still_borrows_the_coin() -> None:
    wide_stop = engine.assess(cons(Direction.SHORT, stop=104.0), account(), market(), limits())
    assert wide_stop.allowed and wide_stop.plan is not None
    assert wide_stop.plan.leverage == D("1.0") and wide_stop.plan.borrow


def test_pause_until_is_72_hours() -> None:
    assert engine.pause_until(NOW) == NOW + timedelta(hours=72)


@pytest.mark.parametrize("asset,expected", [("SPX6900", D("2.5")), ("BTC", D("2.5"))])
def test_spx_cap_only_binds_above_three(asset: str, expected: D) -> None:
    c = cons()
    c = Consensus(
        c.direction,
        c.agreement,
        c.formula_score,
        c.consensus_score,
        c.conviction,
        c.valid_agents,
        c.reason,
        c.proposal.model_copy(update={"asset": asset}) if c.proposal else None,
    )
    a = engine.assess(c, account(), market(), limits())
    assert a.plan is not None and a.plan.leverage == expected


def test_max_track_limits_use_their_own_risk_per_trade() -> None:
    from types import SimpleNamespace

    from app.risk.apply import limits_for_max
    from app.risk.engine import Limits

    lim = Limits(
        capital_max=D(10000),
        risk_per_trade=D("0.005"),
        capital_share=D("0.2"),
        leverage_max=D(20),
        leverage_max_spx6900=D(10),
        gross_exposure_max=D(2),
        depth_cap=D("0.05"),
        day_loss_stop=D("-0.02"),
        drawdown_pause=D("-0.10"),
        emergency_brake=D("-0.20"),
    )
    cfg = SimpleNamespace(trading=SimpleNamespace(risk_per_trade_max=D("0.025")))
    mx = limits_for_max(lim, cfg)  # type: ignore[arg-type]
    assert mx.risk_per_trade == D("0.025") and mx.leverage_max == lim.leverage_max
    assert lim.risk_per_trade == D("0.005")  # the primary's limits are untouched


def test_correlation_cap_sizes_same_direction_trades_into_the_room_left() -> None:
    from app.risk.exposure import beta_from_returns, net_beta_exposure

    # betas: a coin moving 1.5× BTC gets 1.5; too little data → 1
    btc = [0.01, -0.02, 0.03, -0.01, 0.02, -0.03, 0.01, 0.02, -0.02, 0.01, 0.03, -0.01]
    assert beta_from_returns([x * 1.5 for x in btc], btc) == D("1.5")
    assert beta_from_returns([0.01, 0.02], [0.01, 0.02]) == D(1)
    # three same-direction longs at beta ≈ 1 on a 10k book: net 1.2× equity
    net = net_beta_exposure(
        [("long", D(4000), "BTC"), ("long", D(4000), "ETH"), ("long", D(4000), "SOL")],
        {"BTC": D(1), "ETH": D(1), "SOL": D(1)},
    )
    assert net == D(12000)
    # the next long fits only into the 0.3× (3,000) left under the 1.5× cap
    a = engine.assess(cons(), account(net_beta_exposure=net), market(), limits())
    assert a.allowed and a.plan is not None and a.plan.notional <= D(3000)
    assert any(h.rule == "correlated_exposure" for h in a.hits)
    # a short against the net is not capped by it
    s = engine.assess(
        cons(direction=Direction.SHORT, stop=103.0, target=94.0),
        account(net_beta_exposure=net),
        market(),
        limits(),
    )
    assert not any(h.rule == "correlated_exposure" for h in s.hits)
    # at the cap, nothing fits: blocked
    full = engine.assess(cons(), account(net_beta_exposure=D(15000)), market(), limits())
    assert not full.allowed and any(h.rule == "no_room" for h in full.hits)
