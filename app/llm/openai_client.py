"""OpenAI provider: structured output via the Responses API with a pydantic schema."""

from __future__ import annotations

from typing import Any, cast

import openai
from pydantic import BaseModel, SecretStr

from app.llm.base import Image, LLMError, Usage


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: SecretStr) -> None:
        self._client = openai.AsyncOpenAI(api_key=api_key.get_secret_value(), max_retries=0)

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
            {"type": "input_image", "image_url": f"data:{img.media_type};base64,{img.data_b64}"}
            for img in images or []
        ]
        content.append({"type": "input_text", "text": user_text})
        try:
            response = await self._client.with_options(timeout=timeout_s).responses.parse(
                model=model,
                instructions=system,
                input=cast(Any, [{"role": "user", "content": content}]),
                text_format=schema,
                max_output_tokens=max_output_tokens,
                reasoning=cast(Any, {"effort": effort}),
            )
        except openai.APIError as exc:
            raise LLMError(f"openai {model}: {type(exc).__name__}: {exc}") from exc
        parsed = response.output_parsed
        if parsed is None:
            raise LLMError(f"openai {model}: no parsed output (status {response.status})")
        usage = response.usage
        return parsed, Usage(
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cached_input_tokens=(
                usage.input_tokens_details.cached_tokens
                if usage and usage.input_tokens_details
                else 0
            ),
        )
