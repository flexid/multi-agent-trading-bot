"""Formula anchor and consensus (SPEC §7.3–7.4). Pure functions, fully unit-tested."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.agents.schema import AgentOutput
from app.decision.pm import Direction, Proposal

WEIGHTS = {
    "indicators": 0.25,
    "chart_patterns": 0.20,
    "polymarket": 0.20,
    "macro": 0.20,  # × coupling
    "x_sentiment": 0.15,
}
MIN_VALID_AGENTS = 3
CLAMP = 0.4


class Agreement(StrEnum):
    AGREE = "agree"  # same non-flat direction
    MAJORITY = "majority"  # two of three call the same direction, the third is flat
    PARTIAL = "partial"  # one directional, one flat: no trade
    OPPOSITE = "opposite"  # long vs short
    FLAT = "flat"  # both flat
    FAILED = "failed"  # a PM gave nothing valid


@dataclass(frozen=True)
class Consensus:
    direction: Direction
    agreement: Agreement
    formula_score: float | None
    consensus_score: float | None
    conviction: float | None
    valid_agents: int
    reason: str
    proposal: Proposal | None  # the directional proposal to size (merged when both agree)


def weights_for(base: dict[str, float], overrides: dict[str, float] | None) -> dict[str, float]:
    """The tuned weights with an asset's overrides on top (owner 2026-10-09: SPX6900 leans
    on X sentiment). ``formula_score`` renormalizes over the valid agents."""
    return {**base, **(overrides or {})}


def formula_score(
    outputs: list[AgentOutput], coupling: float | None, weights: dict[str, float] | None = None
) -> tuple[float | None, int]:
    """Weighted sum over valid agents, macro scaled by coupling, weights renormalized.
    ``weights`` defaults to the spec's start weights; the cycle passes the tuned ones."""
    valid = [o for o in outputs if o.valid]
    if len(valid) < MIN_VALID_AGENTS:
        return None, len(valid)
    table = weights or WEIGHTS
    total = 0.0
    weighted = 0.0
    for o in valid:
        w = table.get(o.agent, 0.0)
        if o.agent == "macro":
            w *= coupling if coupling is not None else 0.0
        weighted += w * o.score
        total += w
    return (weighted / total if total > 0 else 0.0), len(valid)


CHART_READINGS = ("indicators", "chart_patterns")
X_READINGS = ("x_sentiment", "catalysts")
CHARTS_FLAT = 0.05  # below this the charts say nothing


def charts_first_score(
    outputs: list[AgentOutput], coupling: float | None, weights: dict[str, float] | None = None
) -> float | None:
    """A shadow of the formula score in which price leads and X only confirms (owner
    2026-10-10, logged beside the real score and compared at the review; it decides
    nothing).

    The charts' direction is the weighted mean of the tape and the chart-pattern readings.
    - X sentiment counts only when it points the same way as the charts; against them, or
      with flat charts, its weight is zero (X never vetoes a setup, never opens a trade).
    - Catalysts: a bullish reading counts only with bullish charts; a bearish one (a hack,
      a delisting, an unlock) counts whenever the charts have a direction, so hard news
      can still brake a long.
    Everything else is weighted as in ``formula_score``."""
    valid = [o for o in outputs if o.valid]
    if len(valid) < MIN_VALID_AGENTS:
        return None
    table = weights or WEIGHTS
    charts = [o for o in valid if o.agent in CHART_READINGS]
    cw = sum(table.get(o.agent, 0.0) for o in charts)
    chart = sum(table.get(o.agent, 0.0) * o.score for o in charts) / cw if cw > 0 else 0.0
    side = 0 if abs(chart) < CHARTS_FLAT else (1 if chart > 0 else -1)
    total = 0.0
    weighted = 0.0
    for o in valid:
        w = table.get(o.agent, 0.0)
        if o.agent == "macro":
            w *= coupling if coupling is not None else 0.0
        # a neutral X reading stays in, as in the real score: only opposition is dropped
        if o.agent == "x_sentiment" and (side == 0 or o.score * side < 0):
            continue
        if o.agent == "catalysts" and (side == 0 or (o.score > 0 and side < 0)):
            continue
        weighted += w * o.score
        total += w
    return weighted / total if total > 0 else 0.0


def _merge(a: Proposal, b: Proposal) -> Proposal:
    """Conviction-weighted merge of two same-direction proposals' geometry."""
    wa, wb = max(a.conviction, 1e-6), max(b.conviction, 1e-6)

    def mix(x: float | None, y: float | None) -> float | None:
        if x is None or y is None:
            return x if y is None else y
        return (x * wa + y * wb) / (wa + wb)

    return a.model_copy(
        update={
            "entry_low": mix(a.entry_low, b.entry_low),
            "entry_high": mix(a.entry_high, b.entry_high),
            "stop": mix(a.stop, b.stop),
            "target": mix(a.target, b.target),
            "max_hold_hours": int(round(mix(a.max_hold_hours, b.max_hold_hours) or 24)),
            "score": (a.score * wa + b.score * wb) / (wa + wb),
            "conviction": min(a.conviction, b.conviction),
            "reasons": (a.reasons + b.reasons)[:3],
        }
    )


def _merge_all(proposals: list[Proposal]) -> Proposal:
    merged = proposals[0]
    for other in proposals[1:]:
        merged = _merge(merged, other)
    return merged


def consensus(
    outputs: list[AgentOutput],
    coupling: float | None,
    pm1: Proposal | None,
    pm2: Proposal | None,
    weights: dict[str, float] | None = None,
    *,
    pm3: Proposal | None = None,
    seats: int = 2,
) -> Consensus:
    """With two seats both managers must agree. With three seats (owner 2026-10-11, logic
    v6) a trade needs at least two managers calling the same direction and nobody calling
    the opposite one: all who answered agree → AGREE (full size); two agree and the third
    is flat → MAJORITY (the risk engine halves the size). A manager that gave no answer is
    left out, so two that agree still decide; one answer alone never does."""
    f_score, n_valid = formula_score(outputs, coupling, weights)
    flat = Direction.FLAT

    def failed(reason: str) -> Consensus:
        return Consensus(flat, Agreement.FAILED, f_score, None, None, n_valid, reason, None)

    if n_valid < MIN_VALID_AGENTS:
        return failed("fewer than 3 valid agents")
    if seats < 3:
        if pm1 is None or pm2 is None:
            return failed("a PM gave no valid proposal")
        answers = [pm1, pm2]
    else:
        answers = [p for p in (pm1, pm2, pm3) if p is not None]
        if len(answers) < 2:
            return failed("fewer than two PMs answered")
    called = [p for p in answers if p.direction is not flat]
    if not called:
        reason = "both PMs flat" if len(answers) == 2 else "all PMs flat"
        return Consensus(flat, Agreement.FLAT, f_score, None, None, n_valid, reason, None)
    if len({p.direction for p in called}) > 1:
        return Consensus(
            flat, Agreement.OPPOSITE, f_score, None, None, n_valid, "PMs opposite", None
        )
    total = sum(p.conviction for p in answers)
    raw = sum(p.score * p.conviction for p in answers) / total if total > 0 else 0.0
    assert f_score is not None
    clamped = max(f_score - CLAMP, min(f_score + CLAMP, raw))
    side = called[0].direction
    if len(called) == len(answers):
        merged = _merge_all(called)
        return Consensus(
            side, Agreement.AGREE, f_score, clamped, merged.conviction, n_valid, "PMs agree", merged
        )
    if len(called) >= 2:
        merged = _merge_all(called)
        return Consensus(
            side,
            Agreement.MAJORITY,
            f_score,
            clamped,
            merged.conviction,
            n_valid,
            "two of three PMs agree, one flat",
            merged,
        )
    # Only one manager called a direction: no trade (owner, 2026-10-09).
    reason = "one PM flat" if len(answers) == 2 else "only one PM called a direction"
    return Consensus(flat, Agreement.PARTIAL, f_score, clamped, None, n_valid, reason, None)
