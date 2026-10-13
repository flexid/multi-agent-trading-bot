"""The fast brain: Jev judges every closed 1-minute candle against the trader's rulebook
(bot spec §3, §5). Jev returns probabilities and a score; the thresholds live in code."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.arena.rulebook import CONVICTION_LEVELS, QUESTIONS_VERSION, Rulebook, compile_questions
from app.llm import LLMError, judge

log = logging.getLogger("arena.reflex")
TASK = "arena_reflex"


@dataclass(frozen=True)
class Signal:
    choice: str  # long | short | none
    probabilities: dict[str, float]
    conviction: float


@dataclass(frozen=True)
class EarlyExit:
    close: bool
    probability: float


def _answers(result: Any) -> dict[str, Any]:
    answers = getattr(result, "answers", result)
    return dict(answers) if isinstance(answers, dict) else {}


async def decide_entry(
    rb: Rulebook, state: dict[str, Any], *, model: str, trader: str
) -> Signal | None:
    """Flat: which entry rule, if any, the candles satisfy, and how clearly."""
    try:
        result = await judge(
            TASK,
            version=QUESTIONS_VERSION,
            state=state,
            questions=compile_questions(rb),
            model=model,
            asset=trader,
        )
    except LLMError as exc:
        log.warning("reflex %s: %s", trader, exc)
        return None
    a = _answers(result)
    sig, conv = a.get("signal", {}), a.get("conviction", {})
    probs = {k: float(v) for k, v in (sig.get("probabilities") or {}).items()}
    choice = str(sig.get("choice") or "none")
    score = conv.get("score")
    # the score is the probability-weighted level index over the rubric → 0..1
    conviction = (
        0.0 if score is None else max(0.0, min(1.0, float(score) / (len(CONVICTION_LEVELS) - 1)))
    )
    return Signal(choice=choice, probabilities=probs, conviction=conviction)


async def decide_exit(
    rb: Rulebook, state: dict[str, Any], *, side: str, model: str, trader: str
) -> EarlyExit | None:
    """In a position with an early-exit rule: should it close now?"""
    questions = compile_questions(rb, side=side)
    if not questions:
        return None
    try:
        result = await judge(
            TASK,
            version=QUESTIONS_VERSION,
            state=state,
            questions=questions,
            model=model,
            asset=trader,
        )
    except LLMError as exc:
        log.warning("reflex %s exit: %s", trader, exc)
        return None
    a = _answers(result)
    p = float((a.get("early_exit") or {}).get("noul") or 0.0)
    return EarlyExit(close=p >= 0.5, probability=p)
