"""Shared types for the LLM layer. Providers implement ``Provider.complete``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol, TypeVar

from pydantic import BaseModel

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LLMError(Exception):
    """Any failure to obtain a schema-valid answer. Callers treat it as 'no output'."""


@dataclass(frozen=True)
class Image:
    media_type: str  # image/png
    data_b64: str


@dataclass(frozen=True)
class Prompt:
    """A versioned prompt loaded from app/prompts/<name>.md."""

    name: str
    version: int
    system: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0


@dataclass(frozen=True)
class Completion[SchemaT: BaseModel]:
    parsed: SchemaT
    model: str
    usage: Usage
    cost_usd: Decimal
    latency_ms: int
    attempts: int
    finished_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class Provider(Protocol):
    name: str

    async def complete[SchemaT: BaseModel](
        self,
        *,
        model: str,
        system: str,
        user_text: str,
        schema: type[SchemaT],
        images: list[Image] | None,
        max_output_tokens: int,
        timeout_s: float,
        effort: str,
    ) -> tuple[SchemaT, Usage]: ...


def cost_usd(pricing: dict[str, tuple[Decimal, Decimal]], model: str, usage: Usage) -> Decimal:
    rate_in, rate_out = pricing.get(model, (Decimal(0), Decimal(0)))
    million = Decimal(1_000_000)
    return (
        Decimal(usage.input_tokens) * rate_in + Decimal(usage.output_tokens) * rate_out
    ) / million
