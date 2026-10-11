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


BOUNDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")


def _relax(node: Any) -> Any:
    """The schema for xAI's constrained decoding: numeric bounds removed, every property
    required, no extra properties. With bounds in the grammar Grok 4.7 mangles decimals
    (-0.04 comes out as -1.04, a small positive as 0; seen 2026-10-11), so the bounds are
    checked by pydantic after the call instead."""
    if isinstance(node, list):
        return [_relax(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {k: _relax(v) for k, v in node.items() if k not in BOUNDS and k != "default"}
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


def response_format(schema: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema.__name__,
            "schema": _relax(schema.model_json_schema()),
            "strict": True,
        },
    }


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
            completion = await self._client.with_options(timeout=timeout_s).chat.completions.create(
                model=model,
                messages=cast(
                    Any,
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": content if images else user_text},
                    ],
                ),
                response_format=cast(Any, response_format(schema)),
                max_completion_tokens=max_output_tokens,
                # xAI defaults to "high"; the bot's own setting keeps the managers alike
                reasoning_effort=cast(Any, effort),
            )
        except openai.APIError as exc:
            raise LLMError(f"xai {model}: {type(exc).__name__}: {exc}") from exc
        choice = completion.choices[0] if completion.choices else None
        if choice is None:
            raise LLMError(f"xai {model}: no choices returned")
        if choice.finish_reason == "length":
            raise LLMError(f"xai {model}: output cut at max_tokens={max_output_tokens}")
        try:  # the full schema, bounds included, is enforced here
            parsed = schema.model_validate_json(choice.message.content or "")
        except (ValidationError, ValueError) as exc:
            raise LLMError(f"xai {model}: unparsable output: {str(exc)[:200]}") from exc
        usage = completion.usage
        details = usage.prompt_tokens_details if usage else None
        # xAI reports reasoning tokens apart from the completion's; they are counted as
        # output here so the logged cost does not understate the call.
        out_details = usage.completion_tokens_details if usage else None
        reasoning = (out_details.reasoning_tokens or 0) if out_details else 0
        return parsed, Usage(
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=(usage.completion_tokens + reasoning) if usage else 0,
            cached_input_tokens=(details.cached_tokens or 0) if details else 0,
        )
