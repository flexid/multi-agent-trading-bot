from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agents.schema import AgentOutput, Horizon
from app.decision.consensus import CLAMP, Agreement, consensus, formula_score
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
