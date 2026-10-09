"""Haiku summary of the indicator readings (SPEC §6: "the model only summarizes")."""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from app.agents.schema import AgentOutput
from app.llm import LLMError, Prompt, complete

TASK = "indicators"


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(max_length=400)


async def summarize(out: AgentOutput, prompt: Prompt, cycle_id: int | None = None) -> AgentOutput:
    """Return a copy of ``out`` with the model's two sentences appended to the evidence.

    On any model failure the output is returned unchanged: the numbers are the agent.
    """
    payload = {
        "asset": out.asset,
        "score": out.score,
        "confidence": out.confidence,
        "readings": out.evidence,
        "risk_flags": out.risk_flags,
    }
    try:
        result = await complete(
            TASK,
            Summary,
            prompt=prompt,
            user_text=json.dumps(payload),
            cycle_id=cycle_id,
            asset=out.asset,
        )
    except LLMError:
        return out
    return out.model_copy(
        update={"evidence": [*out.evidence, f"summary: {result.parsed.summary}"][:10]}
    )
