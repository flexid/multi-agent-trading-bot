"""Agent output contract (SPEC §6). Every agent, LLM-backed or not, returns this."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

MAX_DATA_AGE_MIN = 30


class Horizon(StrEnum):
    H4 = "4h"
    D1 = "1d"
    D1_3 = "1-3d"
    D3_7 = "3-7d"


class AgentOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str
    asset: str
    score: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    horizon: Horizon
    evidence: list[str] = Field(max_length=10)
    risk_flags: list[str] = Field(default_factory=list, max_length=10)
    data_age_min: int = Field(ge=0)
    computed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def valid(self) -> bool:
        """Inputs older than 30 minutes make the output unusable (SPEC §6)."""
        return self.data_age_min <= MAX_DATA_AGE_MIN
