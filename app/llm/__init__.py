"""Single entry point for every model call (CLAUDE.md › Conventions).

    result = await complete("macro", MacroView, prompt=load_prompt("macro"), user_text=...)

Model ID comes from ``config.models[task]``, the provider from the model's prefix. Every
call is logged to ``llm_calls`` with tokens, cost, prompt version and outcome. One retry on
failure, then ``LLMError``: the caller gets no output and must not trade on a guess.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from app.config import get_config, get_secrets
from app.db.models import LLMCall
from app.db.session import new_session
from app.llm.base import Completion, Image, LLMError, Prompt, Provider, Usage, cost_usd

log = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_VERSION_RE = re.compile(r"^<!--\s*version:\s*(\d+)\s*-->\s*\n", re.M)


def load_prompt(name: str) -> Prompt:
    """Prompts start with ``<!-- version: N -->``; bump it whenever the text changes."""
    text = (PROMPTS_DIR / f"{name}.md").read_text()
    match = _VERSION_RE.match(text)
    if match is None:
        raise ValueError(f"prompt {name} lacks a version header")
    return Prompt(name=name, version=int(match.group(1)), system=text[match.end() :].strip())


def provider_for(model: str) -> Provider:
    if model.startswith("claude-"):
        return _anthropic()
    if model.startswith("gpt-"):
        return _openai()
    raise LLMError(f"no provider for model {model}")


@lru_cache(maxsize=1)
def _anthropic() -> Provider:
    from app.llm.anthropic_client import AnthropicProvider

    return AnthropicProvider(get_secrets().anthropic_api_key)


@lru_cache(maxsize=1)
def _openai() -> Provider:
    from app.llm.openai_client import OpenAIProvider

    return OpenAIProvider(get_secrets().openai_api_key)


async def complete[SchemaT: BaseModel](
    task: str,
    schema: type[SchemaT],
    *,
    prompt: Prompt,
    user_text: str,
    images: list[Image] | None = None,
    model: str | None = None,
    cycle_id: int | None = None,
    asset: str | None = None,
) -> Completion[SchemaT]:
    cfg = get_config()
    model = model or getattr(cfg.models, task)
    provider = provider_for(model)
    started = time.monotonic()
    error: str | None = None
    attempts = 0
    for attempts in (1, 2):
        try:
            parsed, usage = await asyncio.wait_for(
                provider.complete(
                    model=model,
                    system=prompt.system,
                    user_text=user_text,
                    schema=schema,
                    images=images,
                    max_output_tokens=cfg.llm.max_output_tokens,
                    timeout_s=cfg.llm.timeout_s,
                    effort=cfg.llm.effort,
                ),
                timeout=cfg.llm.timeout_s + 5,
            )
        except (LLMError, TimeoutError) as exc:
            error = f"{type(exc).__name__}: {exc}"[:2000]
            log.warning("llm %s attempt %d failed: %s", task, attempts, error)
            continue
        latency = int((time.monotonic() - started) * 1000)
        result = Completion(
            parsed=parsed,
            model=model,
            usage=usage,
            cost_usd=cost_usd(cfg.llm.pricing, model, usage),
            latency_ms=latency,
            attempts=attempts,
        )
        _log_call(task, provider.name, prompt, result, cycle_id, asset, images, user_text, None)
        return result
    latency = int((time.monotonic() - started) * 1000)
    _log_call(
        task,
        provider.name,
        prompt,
        None,
        cycle_id,
        asset,
        images,
        user_text,
        error,
        model,
        latency,
        attempts,
    )
    raise LLMError(f"{task} on {model} failed after {attempts} attempts: {error}")


def _log_call(
    task: str,
    provider: str,
    prompt: Prompt,
    result: Completion[BaseModel] | None,
    cycle_id: int | None,
    asset: str | None,
    images: list[Image] | None,
    user_text: str,
    error: str | None,
    model: str | None = None,
    latency_ms: int | None = None,
    attempts: int = 0,
) -> None:
    try:
        with new_session() as session:
            session.add(
                LLMCall(
                    ts=datetime.now(UTC),
                    task=task,
                    asset=asset,
                    cycle_id=cycle_id,
                    provider=provider,
                    model=result.model if result else (model or ""),
                    prompt_name=prompt.name,
                    prompt_version=prompt.version,
                    input_chars=len(user_text),
                    images=len(images or []),
                    input_tokens=result.usage.input_tokens if result else 0,
                    output_tokens=result.usage.output_tokens if result else 0,
                    cached_input_tokens=result.usage.cached_input_tokens if result else 0,
                    cost_usd=result.cost_usd if result else 0,
                    latency_ms=result.latency_ms if result else (latency_ms or 0),
                    attempts=result.attempts if result else attempts,
                    ok=result is not None,
                    error=error,
                    output=json.loads(result.parsed.model_dump_json()) if result else None,
                )
            )
            session.commit()
    except Exception:  # logging must never take the call down with it
        log.exception("could not log llm call %s", task)


__all__ = ["Completion", "Image", "LLMError", "Prompt", "Usage", "complete", "load_prompt"]
