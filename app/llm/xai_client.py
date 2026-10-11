"""xAI (Grok) provider: structured output through xAI's OpenAI-compatible API
(``https://api.x.ai/v1``), parsed against a pydantic schema.

Used for the shadow third manager seat (owner 2026-10-11). Same contract as the other
providers: one call, no SDK retries; ``app.llm.complete`` owns the retry and the fail
closed.
"""

from __future__ import annotations

from typing import Any, cast

import openai
from pydantic import BaseModel, SecretStr, ValidationError

from app.llm.base import Image, LLMError, Usage

XAI_BASE_URL = "https://api.x.ai/v1"


class XAIProvider:
    name = "xai"

    def __init__(self, api_key: SecretStr) -> None:
        self._client = openai.AsyncOpenAI(
            api_key=api_key.get_secret_value(), base_url=XAI_BASE_URL, max_retries=0
        )

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
                "type": "image_url",
                "image_url": {"url": f"data:{img.media_type};base64,{img.data_b64}"},
            }
            for img in images or []
        ]
        content.append({"type": "text", "text": user_text})
        try:
            completion = await self._client.with_options(
                timeout=timeout_s
            ).beta.chat.completions.parse(
                model=model,
                messages=cast(
                    Any,
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": content if images else user_text},
                    ],
                ),
                response_format=schema,
                max_completion_tokens=max_output_tokens,
            )
        except openai.APIError as exc:
            raise LLMError(f"xai {model}: {type(exc).__name__}: {exc}") from exc
        except (ValidationError, ValueError) as exc:  # output cut off or not the schema
            raise LLMError(f"xai {model}: unparsable output: {str(exc)[:200]}") from exc
        choice = completion.choices[0] if completion.choices else None
        if choice is None:
            raise LLMError(f"xai {model}: no choices returned")
        if choice.finish_reason == "length":
            raise LLMError(f"xai {model}: output cut at max_tokens={max_output_tokens}")
        parsed = choice.message.parsed
        if parsed is None:
            raise LLMError(f"xai {model}: no parsed output (finish {choice.finish_reason})")
        usage = completion.usage
        details = usage.prompt_tokens_details if usage else None
        return parsed, Usage(
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            cached_input_tokens=(details.cached_tokens or 0) if details else 0,
        )
