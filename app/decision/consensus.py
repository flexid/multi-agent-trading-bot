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


def consensus(
    outputs: list[AgentOutput],
    coupling: float | None,
    pm1: Proposal | None,
    pm2: Proposal | None,
    weights: dict[str, float] | None = None,
) -> Consensus:
    f_score, n_valid = formula_score(outputs, coupling, weights)
    flat = Direction.FLAT
    if n_valid < MIN_VALID_AGENTS:
        return Consensus(
            flat, Agreement.FAILED, f_score, None, None, n_valid, "fewer than 3 valid agents", None
        )
    if pm1 is None or pm2 is None:
        return Consensus(
            flat,
            Agreement.FAILED,
            f_score,
            None,
            None,
            n_valid,
            "a PM gave no valid proposal",
            None,
        )
    d1, d2 = pm1.direction, pm2.direction
    if d1 is flat and d2 is flat:
        return Consensus(flat, Agreement.FLAT, f_score, None, None, n_valid, "both PMs flat", None)
    if d1 is not flat and d2 is not flat and d1 is not d2:
        return Consensus(
            flat, Agreement.OPPOSITE, f_score, None, None, n_valid, "PMs opposite", None
        )

    w1, w2 = pm1.conviction, pm2.conviction
    raw = (pm1.score * w1 + pm2.score * w2) / (w1 + w2) if (w1 + w2) > 0 else 0.0
    assert f_score is not None
    clamped = max(f_score - CLAMP, min(f_score + CLAMP, raw))

    if d1 is d2:
        merged = _merge(pm1, pm2)
        return Consensus(
            d1, Agreement.AGREE, f_score, clamped, merged.conviction, n_valid, "PMs agree", merged
        )
    # One PM flat, the other directional: no trade (owner, 2026-10-09).
    return Consensus(flat, Agreement.PARTIAL, f_score, clamped, None, n_valid, "one PM flat", None)
