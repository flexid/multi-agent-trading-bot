"""Portfolio managers (SPEC §7.2): same pack, no tools, neither sees the other."""

from __future__ import annotations

import asyncio
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.decision.evidence import EvidencePack
from app.llm import Completion, LLMError, Prompt, complete

TASK_PM = {"pm_1": "pm_1", "pm_2": "pm_2"}
AGENTS = {"indicators", "chart_patterns", "polymarket", "macro", "x_sentiment"}


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset: str
    direction: Direction
    entry_low: float | None = Field(default=None, gt=0)
    entry_high: float | None = Field(default=None, gt=0)
    stop: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)
    max_hold_hours: int | None = Field(default=None, ge=1, le=168)
    score: float = Field(ge=-1, le=1)
    conviction: float = Field(ge=0, le=1)
    reasons: list[str] = Field(min_length=1, max_length=3)
    weighted_up: list[str] = Field(default_factory=list, max_length=5)
    weighted_down: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def _geometry(self) -> Proposal:
        if self.direction is Direction.FLAT:
            return self
        if None in (self.entry_low, self.entry_high, self.stop, self.target, self.max_hold_hours):
            raise ValueError("a directional proposal needs entry zone, stop, target and hold time")
        assert self.entry_low and self.entry_high and self.stop and self.target
        if self.entry_low > self.entry_high:
            raise ValueError("entry_low above entry_high")
        mid = (self.entry_low + self.entry_high) / 2
        if self.direction is Direction.LONG and not (self.stop < mid < self.target):
            raise ValueError("long needs stop < entry < target")
        if self.direction is Direction.SHORT and not (self.target < mid < self.stop):
            raise ValueError("short needs target < entry < stop")
        return self

    @property
    def stop_distance(self) -> float | None:
        """Stop distance as a fraction of the entry mid, for the leverage formula (SPEC §8)."""
        if self.direction is Direction.FLAT or not (
            self.entry_low and self.entry_high and self.stop
        ):
            return None
        mid = (self.entry_low + self.entry_high) / 2
        return abs(mid - self.stop) / mid


class PMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[Proposal] = Field(min_length=1)


def _check_against_pack(response: PMResponse, pack: EvidencePack) -> dict[str, Proposal]:
    """Keep one proposal per pack asset; drop unknown assets and unknown agent names."""
    wanted = {a.asset: a for a in pack.assets}
    out: dict[str, Proposal] = {}
    for p in response.proposals:
        if p.asset not in wanted or p.asset in out:
            continue
        if wanted[p.asset].valid_agents < 3 and p.direction is not Direction.FLAT:
            p = p.model_copy(update={"direction": Direction.FLAT, "conviction": 0.0})
        p = p.model_copy(
            update={
                "weighted_up": [a for a in p.weighted_up if a in AGENTS],
                "weighted_down": [a for a in p.weighted_down if a in AGENTS],
            }
        )
        out[p.asset] = p
    return out


async def ask_pm(
    pm: str,
    pack: EvidencePack,
    prompt: Prompt,
    *,
    model: str | None = None,
    cycle_id: int | None = None,
) -> tuple[dict[str, Proposal], Completion[PMResponse] | None, str | None]:
    """Returns (proposals by asset, completion, error). Any failure means no proposals."""
    try:
        result = await complete(
            TASK_PM[pm],
            PMResponse,
            prompt=prompt,
            user_text=pack.model_dump_json(),
            model=model,
            cycle_id=cycle_id,
        )
    except LLMError as exc:
        return {}, None, str(exc)[:500]
    return _check_against_pack(result.parsed, pack), result, None


async def ask_both(
    pack: EvidencePack, prompt: Prompt, *, models: dict[str, str], cycle_id: int | None = None
) -> dict[str, tuple[dict[str, Proposal], Completion[PMResponse] | None, str | None]]:
    """Both PMs in parallel; keys 'pm_1' and 'pm_2'."""
    results = await asyncio.gather(
        ask_pm("pm_1", pack, prompt, model=models["pm_1"], cycle_id=cycle_id),
        ask_pm("pm_2", pack, prompt, model=models["pm_2"], cycle_id=cycle_id),
    )
    return {"pm_1": results[0], "pm_2": results[1]}
