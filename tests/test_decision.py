from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agents.schema import AgentOutput, Horizon
from app.decision.consensus import CLAMP, Agreement, Consensus, consensus, formula_score
from app.decision.evidence import AssetPack, EvidencePack
from app.decision.pm import Direction, PMResponse, Proposal, _check_against_pack


def out(agent: str, score: float, conf: float = 0.7, age: int = 5) -> AgentOutput:
    return AgentOutput(
        agent=agent,
        asset="BTC",
        score=score,
        confidence=conf,
        horizon=Horizon.D1_3,
        evidence=["e"],
        risk_flags=[],
        data_age_min=age,
    )


def prop(direction: Direction, score: float, conviction: float, **geo: float) -> Proposal:
    base: dict[str, object] = {
        "asset": "BTC",
        "direction": direction,
        "score": score,
        "conviction": conviction,
        "reasons": ["r"],
    }
    if direction is Direction.LONG:
        base |= {
            "entry_low": 99.0,
            "entry_high": 101.0,
            "stop": 95.0,
            "target": 110.0,
            "max_hold_hours": 24,
        }
    elif direction is Direction.SHORT:
        base |= {
            "entry_low": 99.0,
            "entry_high": 101.0,
            "stop": 105.0,
            "target": 90.0,
            "max_hold_hours": 24,
        }
    base |= geo
    return Proposal.model_validate(base)


FIVE = [
    out("indicators", 0.5),
    out("chart_patterns", 0.4),
    out("polymarket", 0.2),
    out("macro", 1.0),
    out("x_sentiment", -0.2),
]


def test_formula_weights_renormalize_and_scale_macro_by_coupling() -> None:
    full, n = formula_score(FIVE, coupling=1.0)
    decoupled, _ = formula_score(FIVE, coupling=0.0)
    assert n == 5
    expected_full = (0.25 * 0.5 + 0.2 * 0.4 + 0.2 * 0.2 + 0.2 * 1.0 + 0.15 * -0.2) / 1.0
    assert full == pytest.approx(expected_full)
    assert decoupled == pytest.approx((0.25 * 0.5 + 0.2 * 0.4 + 0.2 * 0.2 + 0.15 * -0.2) / 0.8)


def test_fewer_than_three_valid_agents_means_no_trade() -> None:
    stale = [out("indicators", 0.9), out("chart_patterns", 0.9), out("macro", 0.9, age=45)]
    c = consensus(stale, 1.0, prop(Direction.LONG, 0.8, 0.9), prop(Direction.LONG, 0.8, 0.9))
    assert c.direction is Direction.FLAT and c.agreement is Agreement.FAILED
    assert c.valid_agents == 2


def test_agreement_gives_conviction_weighted_mean_clamped_to_formula() -> None:
    c = consensus(FIVE, 1.0, prop(Direction.LONG, 1.0, 0.9), prop(Direction.LONG, 0.9, 0.3))
    assert c.direction is Direction.LONG and c.agreement is Agreement.AGREE
    assert c.formula_score is not None and c.consensus_score is not None
    assert c.consensus_score <= c.formula_score + CLAMP + 1e-9
    assert c.conviction == 0.3  # the lower of the two
    assert c.proposal is not None and c.proposal.stop is not None
    assert c.proposal.entry_low is not None and c.proposal.stop < c.proposal.entry_low


def test_opposite_directions_mean_no_trade() -> None:
    c = consensus(FIVE, 1.0, prop(Direction.LONG, 0.8, 0.9), prop(Direction.SHORT, -0.8, 0.9))
    assert c.direction is Direction.FLAT and c.agreement is Agreement.OPPOSITE


def test_one_flat_pm_means_no_trade() -> None:
    c = consensus(FIVE, 1.0, prop(Direction.SHORT, -0.6, 0.9), prop(Direction.FLAT, 0.0, 0.5))
    assert c.direction is Direction.FLAT and c.agreement is Agreement.PARTIAL
    assert c.proposal is None and c.conviction is None


def test_missing_pm_fails_closed() -> None:
    c = consensus(FIVE, 1.0, None, prop(Direction.LONG, 0.8, 0.9))
    assert c.direction is Direction.FLAT and c.agreement is Agreement.FAILED


def test_proposal_geometry_is_validated() -> None:
    with pytest.raises(ValidationError):
        prop(Direction.LONG, 0.5, 0.5, stop=102.0)  # stop above entry on a long
    with pytest.raises(ValidationError):
        prop(Direction.SHORT, 0.5, 0.5, target=120.0)  # target above entry on a short
    with pytest.raises(ValidationError):
        Proposal(asset="BTC", direction=Direction.LONG, score=0.1, conviction=0.1, reasons=["r"])
    assert prop(Direction.LONG, 0.5, 0.5).stop_distance == pytest.approx(0.05)
    assert prop(Direction.FLAT, 0.0, 0.0).stop_distance is None


def test_pack_check_forces_flat_below_three_valid_agents_and_drops_unknowns() -> None:
    pack = EvidencePack(
        as_of=datetime.now(UTC),
        mode="shadow",
        day_pnl_pct=0.0,
        assets=[
            AssetPack(
                asset="BTC",
                spot=100,
                atr_4h=2,
                agents=[],
                valid_agents=2,
                macro_regime=None,
                coupling=None,
                open_position=None,
                recent_decisions=[],
            )
        ],
    )
    response = PMResponse(
        proposals=[
            prop(Direction.LONG, 0.8, 0.9, **{}).model_copy(
                update={"weighted_up": ["indicators", "oracle"]}
            ),
            prop(Direction.LONG, 0.8, 0.9).model_copy(update={"asset": "DOGE"}),
        ]
    )
    checked = _check_against_pack(response, pack)
    assert set(checked) == {"BTC"}
    assert checked["BTC"].direction is Direction.FLAT
    assert checked["BTC"].weighted_up == ["indicators"]


def test_per_asset_weight_overrides_lean_spx6900_on_x_sentiment() -> None:
    from app.decision.consensus import WEIGHTS, weights_for

    base = dict(WEIGHTS)
    spx = weights_for(base, {"x_sentiment": 0.40, "polymarket": 0.0})
    outs = [
        out("indicators", -0.5),
        out("chart_patterns", -0.5),
        out("x_sentiment", 1.0),
        out("polymarket", -1.0),
    ]
    plain, _ = formula_score(outs, None, weights_for(base, None))
    leaning, _ = formula_score(outs, None, spx)
    assert plain is not None and leaning is not None and leaning > plain
    assert weights_for(base, None) == base  # no override: the tuned weights as they are


def _out(agent: str, score: float, valid: bool = True) -> AgentOutput:
    from app.agents.schema import Horizon

    return AgentOutput(
        agent=agent,
        asset="ENA",
        score=score,
        confidence=0.6,
        horizon=Horizon.D1,
        evidence=["e"],
        data_age_min=0 if valid else 99,
    )


def test_charts_first_lets_x_confirm_but_never_veto_or_open() -> None:
    from app.decision.consensus import charts_first_score, formula_score

    w = {"indicators": 0.25, "chart_patterns": 0.25, "x_sentiment": 0.25, "catalysts": 0.25}
    up = [_out("indicators", 0.4), _out("chart_patterns", 0.4)]
    # X agrees: counted, identical to the real formula
    agree = [*up, _out("x_sentiment", 0.8)]
    assert charts_first_score(agree, None, w) == pytest.approx(formula_score(agree, None, w)[0])
    # X disagrees: dropped, so the chart setup stands where the real score is dragged down
    veto = [*up, _out("x_sentiment", -0.8)]
    assert charts_first_score(veto, None, w) == pytest.approx(0.4)
    assert formula_score(veto, None, w)[0] == pytest.approx(0.0)
    # X neutral: kept, so the two scores are identical (no difference without opposition)
    neutral = [*up, _out("x_sentiment", 0.0)]
    assert charts_first_score(neutral, None, w) == pytest.approx(formula_score(neutral, None, w)[0])
    # flat charts: X alone opens nothing
    flat = [_out("indicators", 0.01), _out("chart_patterns", -0.01), _out("x_sentiment", 0.9)]
    assert charts_first_score(flat, None, w) == pytest.approx(0.0)
    assert formula_score(flat, None, w)[0] == pytest.approx(0.3)
    # hard news brakes a long; a bullish catalyst does not rescue a short
    brake = [*up, _out("catalysts", -0.6)]
    assert charts_first_score(brake, None, w) == pytest.approx((0.4 * 0.5 - 0.6 * 0.25) / 0.75)
    down = [_out("indicators", -0.4), _out("chart_patterns", -0.4), _out("catalysts", 0.6)]
    assert charts_first_score(down, None, w) == pytest.approx(-0.4)
    # too few valid readings: no score, like the formula
    assert charts_first_score([_out("indicators", 0.4), _out("x_sentiment", 0.4)], None, w) is None


def test_charts_first_comparison_counts_changed_calls_and_called_returns() -> None:
    from app.decision.charts_first import call, summarize

    assert (call(0.2), call(-0.2), call(0.05)) == (1, -1, 0)
    rows = [
        (0.30, 0.30, 0.02),  # same call, right
        (0.00, 0.40, 0.03),  # X held the real score flat; charts first called it, right
        (-0.20, 0.20, -0.01),  # opposite calls; the real one was right
        (0.15, 0.15, None),  # no return yet
    ]
    s = summarize(rows)
    assert (s["decisions"], s["calls_differ"], s["with_return"]) == (4, 2, 3)
    assert s["calls_differ_share"] == 0.5
    assert s["formula_calls"] == 2 and s["formula_called_return"] == pytest.approx(0.015)
    assert s["charts_first_calls"] == 3
    assert s["charts_first_called_return"] == pytest.approx((0.02 + 0.03 - 0.01) / 3)
    assert s["ic_formula"] is None and s["ic_charts_first"] is None  # fewer than thirty
    assert summarize([])["calls_differ_share"] is None


def test_solo_calls_show_what_a_veto_cost_or_saved() -> None:
    from app.decision.solo_calls import summarize

    f, a = "fable", "astra"
    rows = [
        (f, "short", a, "flat", -0.01, -0.03),  # solo short, price fell: the veto cost 3%
        (f, "short", a, "flat", 0.00, 0.01),  # solo short, price rose: the veto saved 1%
        (f, "flat", a, "long", None, None),  # solo by the other, no price yet
        (f, "long", a, "long", 0.01, 0.02),  # agreed
        (f, "long", a, "short", 0.01, 0.02),  # opposite: no call
        (f, "flat", a, "flat", 0.01, 0.02),
    ]
    s = summarize(rows)
    assert (s["decisions"], s["solo"], s["agreed"], s["opposite"]) == (6, 3, 1, 1)
    by = {(p["caller"], p["flat"]): p for p in s["pairs"]}
    fa = by[(f, a)]
    assert fa["calls"] == 2 and fa["d1"]["n"] == 2
    assert fa["d1"]["mean"] == pytest.approx(0.01) and fa["d1"]["won"] == 0.5
    assert fa["h4"]["mean"] == pytest.approx(0.005)
    assert by[(a, f)]["calls"] == 1 and by[(a, f)]["d1"]["mean"] is None
    assert s["agree"]["d1"]["mean"] == pytest.approx(0.02)
    assert s["flat_share"] == {a: pytest.approx(3 / 6), f: pytest.approx(2 / 6)}
    assert summarize([])["pairs"] == []


def test_three_ways_to_decide() -> None:
    from app.decision.three_seat import Seat, by_threshold, summarize, two_of_three, two_of_two

    short, flat, long_ = Seat("short", -0.2), Seat("flat", -0.2), Seat("long", 0.2)
    assert two_of_two(short, short) == -1 and two_of_two(short, flat) == 0
    assert two_of_three(short, flat, short) == (-1, 0.5)  # the cautious one no longer vetoes
    assert two_of_three(short, short, short) == (-1, 1.0)
    assert two_of_three(short, short, long_) == (0, 0.0)  # anyone opposite: nothing
    assert two_of_three(short, flat, flat) == (0, 0.0)
    assert by_threshold(short, flat) == -1  # scores decide, whatever direction was written
    assert by_threshold(short, Seat("flat", -0.05)) == 0 and by_threshold(short, long_) == 0
    quiet = Seat("flat", 0.0)
    rows: list[tuple[Seat, Seat, Seat, float | None, float | None]] = [
        (short, flat, short, -0.01, -0.02),  # only two-of-three and the threshold trade it
        (short, short, short, 0.005, 0.01),  # everyone trades it, and it loses
        (quiet, quiet, quiet, 0.0, 0.0),
    ]
    s = summarize(rows)
    r = s["rules"]
    assert (r["two_of_two"]["trades"], r["two_of_three"]["trades"]) == (1, 2)
    assert r["three_of_three"]["trades"] == 1 and r["threshold"]["trades"] == 2
    assert r["two_of_two"]["d1"]["mean"] == pytest.approx(-0.01)
    assert r["two_of_three"]["d1"]["mean"] == pytest.approx(0.005)
    assert r["two_of_three"]["d1"]["won"] == 0.5
    assert s["flat_share"] == [pytest.approx(1 / 3), pytest.approx(2 / 3), pytest.approx(1 / 3)]
    assert summarize([])["flat_share"] == [None, None, None]


def test_grok_models_route_to_xai_and_the_third_seat_is_configured() -> None:
    from pathlib import Path

    from app import llm
    from app.config import get_config
    from app.decision.pm import TASK_PM

    cfg = get_config()
    assert cfg.models.pm_3.startswith("grok-") and "pm_3" in TASK_PM
    assert cfg.models.pm_3 in cfg.llm.pricing
    assert llm.provider_for(cfg.models.pm_3).name == "xai"
    assert "X_AI" not in Path("scripts/admin_env.sh").read_text()


def test_two_of_three_decides_and_nobody_may_be_opposite() -> None:
    """Logic v6 (owner 2026-10-11): three seats, two of three with nobody opposite."""
    short = prop(Direction.SHORT, -0.3, 0.6)
    weak_short = prop(Direction.SHORT, -0.2, 0.5, stop=106.0)
    flat = prop(Direction.FLAT, -0.1, 0.4)
    long_ = prop(Direction.LONG, 0.3, 0.6)

    def run(a: Proposal | None, b: Proposal | None, c: Proposal | None) -> Consensus:
        return consensus(FIVE, 0.5, a, b, pm3=c, seats=3)

    # all three agree: a full agreement
    all3 = run(short, weak_short, short)
    assert (all3.direction, all3.agreement) == (Direction.SHORT, Agreement.AGREE)
    # two agree, the cautious one is flat: no veto any more, a majority
    maj = run(short, flat, weak_short)
    assert (maj.direction, maj.agreement) == (Direction.SHORT, Agreement.MAJORITY)
    assert maj.proposal is not None and maj.proposal.stop == pytest.approx(105.45, abs=0.01)
    assert maj.conviction == 0.5 and maj.reason == "two of three PMs agree, one flat"
    # anyone opposite: nothing, even with two on one side
    opp = run(short, weak_short, long_)
    assert (opp.direction, opp.agreement) == (Direction.FLAT, Agreement.OPPOSITE)
    # one call alone never trades
    solo = run(short, flat, flat)
    assert (solo.direction, solo.agreement) == (Direction.FLAT, Agreement.PARTIAL)
    assert run(flat, flat, flat).agreement is Agreement.FLAT
    # a missing manager is left out: the other two must agree, and then it is full agreement
    assert run(short, None, weak_short).agreement is Agreement.AGREE
    assert run(short, flat, None).agreement is Agreement.PARTIAL
    assert run(None, None, short).agreement is Agreement.FAILED  # never one model alone
    # with two seats the old rule stands, whatever a third argument says
    old = consensus(FIVE, 0.5, short, flat, pm3=weak_short, seats=2)
    assert old.agreement is Agreement.PARTIAL


async def test_the_third_seat_has_a_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from app.decision import pm as pm_mod

    async def fake_ask(
        seat: str, *a: object, **k: object
    ) -> tuple[dict[str, Proposal], None, None]:
        if seat == "pm_3":
            await asyncio.sleep(5)
        return {"BTC": prop(Direction.SHORT, -0.3, 0.6)}, None, None

    monkeypatch.setattr(pm_mod, "ask_pm", fake_ask)
    res = await pm_mod.ask_three(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        models={"pm_1": "a", "pm_2": "b", "pm_3": "c"},
        deadline_s=0.05,
    )
    assert "BTC" in res["pm_1"][0] and "BTC" in res["pm_2"][0]
    assert res["pm_3"][0] == {} and "no answer within" in (res["pm_3"][2] or "")
