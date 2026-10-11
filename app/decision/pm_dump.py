"""Dump what the portfolio managers receive (owner 2026-10-11): the system prompt and the
user message, the same for both managers and both seats.

    python -m app.decision.pm_dump            # the latest cycle
    python -m app.decision.pm_dump 15         # a given cycle

Cycles from 2026-10-11 on store the exact evidence pack. For an older cycle the pack is
rebuilt from the stored readings: the readings are exact, but the spot, the ATR and the
"since the last decision" figures are computed from today's candles, and the dump says so.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from sqlalchemy import select

from app.agents.schema import AgentOutput, Horizon
from app.config import get_config
from app.db.models import AgentOutputRecord, Cycle, LLMCall
from app.db.session import new_session
from app.decision.evidence import build_pack
from app.llm import load_prompt


def pack_for(cycle_id: int | None = None) -> tuple[Cycle, dict[str, Any], bool]:
    """(cycle, pack, exact). ``exact`` is False for a pack rebuilt from stored readings."""
    cfg = get_config()
    with new_session() as s:
        stmt = select(Cycle).order_by(Cycle.id.desc()).limit(1)
        if cycle_id is not None:
            stmt = select(Cycle).where(Cycle.id == cycle_id)
        cycle = s.scalars(stmt).first()
        if cycle is None:
            raise SystemExit("no such cycle")
        if cycle.evidence_pack:
            return cycle, dict(cycle.evidence_pack), True
        rows = s.scalars(
            select(AgentOutputRecord).where(
                AgentOutputRecord.cycle_id == cycle.id, AgentOutputRecord.variant == "main"
            )
        ).all()
        outputs: dict[str, list[AgentOutput]] = {}
        regime: str | None = None
        couplings: dict[str, float] = {}
        for r in rows:
            outputs.setdefault(r.asset, []).append(
                AgentOutput(
                    agent=r.agent,
                    asset=r.asset,
                    score=r.score,
                    confidence=r.confidence,
                    horizon=Horizon(r.horizon),
                    evidence=[str(e) for e in r.evidence or []],
                    risk_flags=[str(f) for f in r.risk_flags or []],
                    data_age_min=r.data_age_min,
                    computed_at=r.computed_at,
                )
            )
            if r.agent == "macro" and r.evidence:
                head = str(r.evidence[0])
                parts = head.split(" ")
                regime = parts[1] if len(parts) > 1 else regime
                try:
                    couplings[r.asset] = float(head.split("coupling ")[1])
                except (IndexError, ValueError):
                    couplings[r.asset] = 0.0
        ordered = {a: outputs[a] for a in cfg.trading.assets if a in outputs}
        ordered.update({a: o for a, o in outputs.items() if a not in ordered})
        pack = build_pack(
            s,
            mode=cycle.mode,
            outputs=ordered,
            symbols={a: cfg.symbol(a) for a in ordered},
            macro_regime=regime,
            couplings=couplings,
            now=cycle.started_at,
        )
        return cycle, json.loads(pack.model_dump_json()), False


def dump(cycle_id: int | None = None) -> str:
    cycle, pack, exact = pack_for(cycle_id)
    prompt = load_prompt("pm")
    with new_session() as s:
        calls = s.scalars(
            select(LLMCall)
            .where(LLMCall.cycle_id == cycle.id, LLMCall.task.in_(["pm_1", "pm_2"]))
            .order_by(LLMCall.id)
        ).all()
    compact = json.dumps(pack, separators=(",", ":"))
    lines = [
        f"# What the managers received, cycle {cycle.id}",
        "",
        f"Cycle {cycle.id}, {cycle.kind}, started {cycle.started_at:%Y-%m-%d %H:%M} UTC, "
        f"mode {cycle.mode}. Both managers, in their own seat and in the shadow seat, get the "
        "same two messages below and nothing else: no tools, no web, no memory.",
        "",
        (
            "The user message is the exact stored input."
            if exact
            else "The user message is REBUILT from the stored readings: every reading is "
            "exact, but spot, ATR and the price change since earlier decisions come from "
            "the candles as they are now, not as they were at the cycle."
        ),
        "",
        "Calls logged for this cycle:",
        *[
            f"- {c.task} {c.model}: {c.input_chars:,} characters in, {c.input_tokens:,} input "
            f"tokens, {c.output_tokens:,} output tokens, ${c.cost_usd:.4f}, "
            f"{c.latency_ms / 1000:.0f} s, {'ok' if c.ok else 'failed'}"
            for c in calls
        ],
        f"- this dump's user message: {len(compact):,} characters",
        "",
        f"## 1. System prompt (app/prompts/pm.md, version {prompt.version})",
        "",
        "```",
        prompt.system,
        "```",
        "",
        "## 2. User message (the evidence pack, sent as compact JSON; indented here)",
        "",
        "```json",
        json.dumps(pack, indent=1),
        "```",
        "",
        "## 3. What they must return",
        "",
        "A JSON object validated against the PMResponse schema: one proposal per asset "
        "with direction, entry zone, stop, target, max_hold_hours, score, conviction, three "
        "reasons, and the agents weighted up or down. Anything else is rejected.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    print(dump(int(args[0]) if args else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
