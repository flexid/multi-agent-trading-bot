"""Anthropic provider: structured output via ``messages.parse`` with a pydantic schema."""

from __future__ import annotations

from typing import Any, cast

import anthropic
from pydantic import BaseModel, SecretStr

from app.llm.base import Image, LLMError, Usage


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: SecretStr) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key.get_secret_value(), max_retries=0)

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
    ) -> tuple[SchemaT, Usage]:
        content: list[Any] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": img.media_type, "data": img.data_b64},
            }
            for img in images or []
        ]
        content.append({"type": "text", "text": user_text})
        try:
            response = await self._client.with_options(timeout=timeout_s).messages.parse(
                model=model,
                max_tokens=max_output_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
                output_format=schema,
                output_config=cast(Any, {"effort": effort}),
            )
        except anthropic.APIError as exc:
            raise LLMError(f"anthropic {model}: {type(exc).__name__}: {exc}") from exc
        if response.stop_reason == "refusal":
            raise LLMError(f"anthropic {model}: refusal")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError(f"anthropic {model}: no parsed output (stop {response.stop_reason})")
        usage = Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cached_input_tokens=response.usage.cache_read_input_tokens or 0,
        )
        return parsed, usage
