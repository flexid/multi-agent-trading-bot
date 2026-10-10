"""TypeSafe Jev provider: typed judgments (Choice, Noul, Score) over a JSON state.

Jev is a System One model: it returns probabilities, not text, so it has no prompt file.
The questions are versioned in code (``app/agents/jev_questions.py``). The SDK's own retry
is off; ``app.llm.judge`` does the one retry and the fail-closed, like every other provider.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr

from app.llm.base import LLMError, Usage

log = logging.getLogger(__name__)

JSON = dict[str, Any] | list[Any] | str


@dataclass(frozen=True)
class Judgment:
    """One answer per question id, as plain dicts: ``{"type": "choice", "choice": ...,
    "probabilities": {...}, "confidence": ...}``, ``{"type": "noul", "noul": p}`` or
    ``{"type": "score", "score": s, "probabilities": {...}, "confidence": ...}``."""

    model: str
    answers: dict[str, dict[str, Any]]
    usage: Usage


class JevProvider:
    name = "typesafe"

    def __init__(self, api_key: SecretStr) -> None:
        from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

        self._client = AsyncTypeSafeClient(
            api_key=api_key.get_secret_value() or None,
            retry=RetryPolicy(max_retries=0),  # app.llm.judge retries once, then fails closed
        )

    async def judge(
        self, *, model: str, state: JSON, questions: dict[str, Any], timeout_s: float
    ) -> Judgment:
        from typesafe_sdk import TypeSafeAPIError, TypeSafeError

        try:
            response = await self._client.system_one(
                state, questions, model=model, timeout=timeout_s
            )
        except TypeSafeAPIError as exc:
            status = getattr(exc, "status", None)
            raise LLMError(f"jev {model}: {type(exc).__name__} (HTTP {status}): {exc}") from exc
        except TypeSafeError as exc:
            raise LLMError(f"jev {model}: {type(exc).__name__}: {exc}") from exc
        answers = {key: ans.model_dump() for key, ans in response.answers.items()}
        missing = set(questions) - set(answers)
        if missing:
            raise LLMError(f"jev {model}: no answer for {sorted(missing)}")
        return Judgment(
            model=response.model,
            answers=answers,
            usage=Usage(
                input_tokens=response.usage.input_tokens or 0,
                output_tokens=response.usage.output_tokens or 0,
            ),
        )
