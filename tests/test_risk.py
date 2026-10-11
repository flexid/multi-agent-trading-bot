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
    # depth cap: 5% of a 60k book on a 2k share (20% of 10k) allows 1.5x, floored to halves
    assert lev.compute(
        inputs(stop_distance=D("0.002"), depth_quote_2pct=D(60_000), capital=D(10_000))
    ).leverage == D("1.5")
    assert lev.compute(
        inputs(stop_distance=D("0.002"), depth_quote_2pct=D(5_000_000), capital=D(10_000))
    ).leverage == D(10)
    assert lev.compute(inputs(stop_distance=D("0.002"), event_today=True)).leverage == D(2)
    assert lev.compute(inputs(stop_distance=D("0.002"), risk_off_coupled=True)).leverage == D(2)
    assert lev.compute(inputs(stop_distance=D("0.002"), ceiling=D(2))).leverage == D(2)
    assert lev.compute(inputs(stop_distance=D("0.002"), fng_extreme=True)).leverage == D(2)
    r = lev.compute(inputs(stop_distance=D("0.005"), atr_extreme=True))
    assert r.leverage == D("2.5") and ("atr_extreme", D("2.5")) in r.caps
    assert lev.compute(inputs(stop_distance=D("0.005"), two_losing_days=True)).leverage == D("2.5")
    assert lev.compute(inputs(stop_distance=D("0.005"), half_risk=True)).leverage == D("2.5")


def test_disagreement_or_low_conviction_means_1x_no_borrow() -> None:
    r = lev.compute(inputs(conviction=D("0.39")))
    assert r.leverage == D(1) and not r.borrow_allowed
    assert lev.compute(inputs(conviction=D("0.4"))).borrow_allowed  # the bar since logic v7


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
    a = engine.assess(cons(conviction=0.3), account(), market(), limits())
    assert a.allowed and a.plan is not None
    assert a.plan.leverage == D("1.0") and not a.plan.borrow


def test_agreed_short_at_1x_still_borrows_the_coin() -> None:
    wide_stop = engine.assess(cons(Direction.SHORT, stop=104.0), account(), market(), limits())
    assert wide_stop.allowed and wide_stop.plan is not None
    assert wide_stop.plan.leverage == D("1.0") and wide_stop.plan.borrow


def test_pause_until_is_72_hours() -> None:
    assert engine.pause_until(NOW) == NOW + timedelta(hours=72)


def test_depth_cap_per_asset_binds_on_a_thin_book() -> None:
    """A thin book caps leverage so the asset's share of capital stays within depth_cap of
    the ±2% depth (owner 2026-10-10, replaces the SPX6900 cap)."""
    deep = engine.assess(cons(), account(), market(depth_quote_2pct=D(5_000_000)), limits())
    thin = engine.assess(cons(), account(), market(depth_quote_2pct=D(60_000)), limits())
    assert deep.allowed and thin.allowed and deep.plan is not None and thin.plan is not None
    assert thin.plan.leverage < deep.plan.leverage
    assert any(h.rule == "leverage_cap:depth" for h in thin.hits)


def test_max_track_limits_use_their_own_risk_per_trade() -> None:
    from types import SimpleNamespace

    from app.risk.apply import limits_for_max
    from app.risk.engine import Limits

    lim = Limits(
        capital_max=D(10000),
        risk_per_trade=D("0.005"),
        capital_share=D("0.2"),
        leverage_max=D(20),
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


def test_a_two_of_three_majority_trades_at_half_the_risk() -> None:
    """Logic v6: two managers agree and the third is flat → half size; leverage and
    borrowing are judged like a full agreement."""
    full = engine.assess(cons(), account(), market(), limits())
    half = engine.assess(cons(agreement=Agreement.MAJORITY), account(), market(), limits())
    assert full.allowed and half.allowed and full.plan and half.plan
    assert half.plan.notional == pytest.approx(full.plan.notional / 2, rel=0.02)
    assert half.plan.leverage == full.plan.leverage  # the size halves, not the leverage
    # also at 1x, where halving the risk alone would have changed nothing
    f1 = engine.assess(cons(stop=80.0, target=150.0), account(), market(), limits())
    h1 = engine.assess(
        cons(agreement=Agreement.MAJORITY, stop=80.0, target=150.0), account(), market(), limits()
    )
    assert f1.plan and h1.plan and f1.plan.leverage == 1
    assert h1.plan.notional == pytest.approx(f1.plan.notional / 2, rel=0.02)
    assert any(h.rule == "majority_half_size" for h in half.hits)
    assert not any(h.rule == "majority_half_size" for h in full.hits)
    assert not any(h.rule == "disagree_or_low_conviction" for h in half.hits)


def test_a_short_at_conviction_point_four_can_borrow_since_v7() -> None:
    ok = engine.assess(
        cons(Direction.SHORT, conviction=0.4, stop=104.0), account(), market(), limits()
    )
    assert ok.allowed and ok.plan is not None and ok.plan.borrow
    low = engine.assess(
        cons(Direction.SHORT, conviction=0.39, stop=104.0), account(), market(), limits()
    )
    assert not low.allowed and any(h.rule == "short_needs_borrow" for h in low.hits)


def test_aggressive_mode_pushes_risk_and_leverage_inside_hard_bounds() -> None:
    from app.config import AggressiveConfig, get_config
    from app.risk.apply import (
        NEVER,
        RISK_HARD_MAX,
        aggressive_account,
        aggressive_limits,
        cap_hold,
        sleeve_values,
    )

    cfg = get_config()
    agg = cfg.aggressive
    blue, alts = cfg.sleeves["bluechips"], cfg.sleeves["alts"]
    assert sleeve_values(blue, None) == (
        blue.risk_per_trade,
        blue.day_loss_stop,
        blue.net_beta_exposure_max,
    )
    risk, stop, beta = sleeve_values(blue, agg)
    assert risk == D("0.025") and stop == NEVER and beta == D("3.0")
    assert sleeve_values(alts, agg)[0] == D("0.0125")
    # the hard bound on risk holds whatever the profile says
    assert sleeve_values(blue, AggressiveConfig(risk_multiplier=D(50)))[0] == RISK_HARD_MAX
    assert aggressive_limits(limits(), None) == limits()
    pushed = aggressive_limits(limits(), agg)
    assert pushed.risk_per_trade == limits().risk_per_trade * D("2.5")
    assert pushed.net_beta_total_max == D("3.0")
    # no trade is held longer than a day
    long_hold = cons()
    held = cap_hold(replace_hold(long_hold, 72), agg)
    assert held.proposal is not None and held.proposal.max_hold_hours == 24
    assert cap_hold(replace_hold(long_hold, 12), agg).proposal.max_hold_hours == 12  # type: ignore[union-attr]
    assert cap_hold(replace_hold(long_hold, 72), None).proposal.max_hold_hours == 72  # type: ignore[union-attr]
    # the bigger risk sizes a bigger trade at the same stop
    normal = engine.assess(cons(), account(), market(), limits())
    bigger = engine.assess(cons(), account(), market(), pushed)
    assert normal.plan and bigger.plan and bigger.plan.notional > normal.plan.notional
    assert agg.take_profit_equity_pct == D("0.01")
    # live keeps the go-live leverage ramp; paper gets the full ceiling
    assert (
        aggressive_account(account(leverage_ceiling=D(2)), agg, live=False).leverage_ceiling == 10
    )
    assert aggressive_account(account(leverage_ceiling=D(2)), agg, live=True).leverage_ceiling == 2


def test_aggressive_mode_has_no_locks() -> None:
    """Owner 2026-10-11: while the switch is on nothing stops the bot for a loss. A day
    that would close everything, a drawdown that would pause, a loss that would brake and
    a coin already traded three times today all still trade. A brake set by hand stands."""
    from datetime import UTC, datetime, timedelta

    from app.config import get_config
    from app.risk.apply import aggressive_account, aggressive_limits

    agg = get_config().aggressive
    now = datetime.now(UTC)
    bad = account(
        equity=D(6000),  # -40% against 10,000 starting capital and its peak
        day_pnl_pct=D("-0.15"),
        paused_until=now + timedelta(hours=40),
        pause_count_30d=2,
        half_risk=True,
        losing_days_in_row=5,
        trades_today={"BTC": 7},
    )
    assert engine.account_rules(bad, limits(), now)  # normally: stopped
    lim_a = aggressive_limits(limits(), agg)
    acct_a = aggressive_account(bad, agg, live=False)
    assert engine.account_rules(acct_a, lim_a, now) == []
    a = engine.assess(cons(), acct_a, market(), lim_a)
    assert a.allowed and not any(h.rule == "trades_per_day" for h in a.hits)
    braked = aggressive_account(account(emergency_brake=True), agg, live=False)
    assert [h.effect for h in engine.account_rules(braked, lim_a, now)] == ["brake"]


def replace_hold(c: Consensus, hours: int) -> Consensus:
    from dataclasses import replace

    assert c.proposal is not None
    return replace(c, proposal=c.proposal.model_copy(update={"max_hold_hours": hours}))


def test_aggressive_mode_gives_stops_more_room_at_the_same_risk() -> None:
    from app.config import AggressiveConfig, get_config
    from app.risk.apply import widen_stop

    agg = get_config().aggressive
    assert agg.stop_room == D("1.5")
    long_ = cons(stop=98.0)  # entry zone 99-101, middle 100: the stop is 2 away
    wide = widen_stop(long_, agg)
    assert wide.proposal is not None and wide.proposal.stop == pytest.approx(97.0)  # 3 away
    short = widen_stop(cons(Direction.SHORT, stop=103.0, target=94.0), agg)
    assert short.proposal is not None and short.proposal.stop == pytest.approx(104.5)
    assert widen_stop(long_, None) is long_  # mode off: untouched
    assert widen_stop(long_, AggressiveConfig(stop_room=D(1))) is long_
    capped = widen_stop(long_, AggressiveConfig(stop_room=D(50)))  # never more than 3×
    assert capped.proposal is not None and capped.proposal.stop == pytest.approx(94.0)
    # the engine sizes on the new distance: a smaller position, the same money at risk
    lim = limits(risk_per_trade=D("0.025"), hard_stop_atr=D(0), stop_buffer_atr=D(0))
    acct = account(leverage_ceiling=D(10))
    tight = engine.assess(long_, acct, market(), lim)
    roomy = engine.assess(wide, acct, market(), lim)
    assert tight.plan and roomy.plan and roomy.plan.notional < tight.plan.notional
    risk_tight = tight.plan.notional * (tight.plan.entry - tight.plan.stop) / tight.plan.entry
    risk_roomy = roomy.plan.notional * (roomy.plan.entry - roomy.plan.stop) / roomy.plan.entry
    assert risk_roomy == pytest.approx(risk_tight, rel=D("0.1"))
    # a position cannot shrink below 1x, so the stop is not widened past that point
    from app.risk.apply import one_x_distance

    small = limits(risk_per_trade=D("0.005"))  # 1x is reached at 0.005 / 0.2 × 0.8 = 2%
    assert one_x_distance(small) == D("0.02")
    held = widen_stop(long_, agg, one_x_distance(small))
    assert held is long_  # already 2% away: no more room to give
    part = widen_stop(cons(stop=99.0), agg, one_x_distance(small))  # 1% away → 1.5%
    assert part.proposal is not None and part.proposal.stop == pytest.approx(98.5)
