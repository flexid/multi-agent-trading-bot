"""The slow brain: once per round each trader's model writes its rulebook (bot spec §3, §4).
Through the LLM layer: the model from the arena config, the prompt versioned, cost and
latency logged, one retry, then the trader stays flat for the round."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.arena.rulebook import Rulebook
from app.llm import LLMError, complete, load_prompt

log = logging.getLogger("arena.author")
TASK = "arena_author"


def round_context(
    *,
    trader: str,
    round_number: int,
    stats: dict[str, Any],
    last_rulebook: dict[str, Any] | None,
    last_result: dict[str, Any] | None,
    hourly: list[list[float]],
) -> str:
    """What the author is told: its own record, last round's book and result, and the last
    200 hourly candles. No strategy hints."""
    ctx = {
        "trader": trader,
        "round": round_number,
        "your_record": stats,
        "last_round": {"rulebook": last_rulebook, "result": last_result},
        "candles_1h": [[int(r[0]), r[1], r[2], r[3], r[4], r[5]] for r in hourly[-200:]],
        "candle_fields": "[time_ms, open, high, low, close, volume], oldest first",
    }
    return json.dumps(ctx, separators=(",", ":"))


async def write_rulebook(model: str, context: str, *, round_number: int) -> Rulebook | None:
    """None when the model fails twice or answers outside the schema: no rulebook, flat."""
    prompt = load_prompt(TASK)
    try:
        done = await complete(
            TASK,
            Rulebook,
            prompt=prompt,
            user_text=context,
            model=model,
            cycle_id=None,
            asset=f"R{round_number}",
        )
    except LLMError as exc:
        log.warning("author %s failed for round %d: %s", model, round_number, exc)
        return None
    return done.parsed
