"""Weekly improvement memo (owner, 2026-10-09): the facts of the last seven days, computed
in code, plus proposals written by a model for the owner to review. Nothing in here
changes the bot; a proposal becomes a change only when a human applies it.

    python -m app.memo            # build, write logs/memo-<date>.md, email it
    python -m app.memo --no-mail  # build and write only
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Config, get_config
from app.db.models import (
    Cycle,
    FetchRun,
    LLMCall,
    PaperAccount,
    Position,
    RiskRuleHit,
    SuppressedTrigger,
    XPostOut,
)
from app.db.session import new_session

log = logging.getLogger(__name__)

TASK = "memo"
MEMO_DIR = Path("logs")
WEEK = timedelta(days=7)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=80)
    evidence: str = Field(max_length=400)  # which facts, quoted with their numbers
    change: str = Field(max_length=400)  # the concrete change: parameter, prompt, rule
    risk: str = Field(max_length=300)  # what could go wrong if applied
    effort: str  # small | medium | large


class Memo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(max_length=2500)
    keep: list[str] = Field(max_length=5)  # what is working and should not be touched
    proposals: list[Proposal] = Field(max_length=6)


@dataclass(frozen=True)
class TrackStats:
    closed: int
    wins: int
    pnl: float
    profit_factor: float | None
    avg_hold_h: float | None
    reasons: dict[str, int]


def _track_stats(rows: list[Position]) -> TrackStats:
    pnls = [float(p.pnl or 0) for p in rows]
    wins = [x for x in pnls if x > 0]
    losses = [-x for x in pnls if x < 0]
    holds = [
        (p.closed_at - p.opened_at).total_seconds() / 3600
        for p in rows
        if p.closed_at and p.opened_at
    ]
    return TrackStats(
        closed=len(rows),
        wins=len(wins),
        pnl=round(sum(pnls), 2),
        profit_factor=round(sum(wins) / sum(losses), 2) if losses else None,
        avg_hold_h=round(sum(holds) / len(holds), 1) if holds else None,
        reasons=dict(Counter(p.close_reason or "?" for p in rows)),
    )


def pilot_vs_paper(pilot: list[Position], paper: list[Position]) -> list[dict[str, Any]]:
    """Each pilot position next to the primary paper position from the same decision:
    the entry and exit slippage the real exchange showed against the simulator."""
    by_decision = {p.decision_id: p for p in paper if p.decision_id is not None}
    out = []
    for p in pilot:
        twin = by_decision.get(p.decision_id) if p.decision_id is not None else None
        if twin is None or not p.entry_price or not twin.entry_price:
            continue
        sign = 1 if p.direction == "long" else -1
        entry_bps = float((p.entry_price / twin.entry_price - 1) * 10_000) * sign
        exit_bps = (
            float((p.exit_price / twin.exit_price - 1) * 10_000) * -sign
            if p.exit_price and twin.exit_price
            else None
        )
        out.append(
            {
                "asset": p.asset,
                "direction": p.direction,
                "entry_worse_bps": round(entry_bps, 1),  # positive = pilot paid more
                "exit_worse_bps": round(exit_bps, 1) if exit_bps is not None else None,
                "pilot_fees": float(p.fees or 0),
                "pilot_interest": float(p.interest or 0),
                "closed": p.status == "closed",
            }
        )
    return out


def facts(session: Session, cfg: Config, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = now - WEEK
    closed = session.scalars(
        select(Position).where(Position.status == "closed", Position.closed_at >= since)
    ).all()
    by_track = {
        t: _track_stats([p for p in closed if p.track == t]).__dict__
        for t in ("primary", "max", "pilot", "live", "inverse")
    }
    pilot_rows = session.scalars(select(Position).where(Position.track == "pilot")).all()
    paper_rows = session.scalars(select(Position).where(Position.track == "primary")).all()
    open_rows = session.scalars(
        select(Position).where(Position.status.in_(["open", "closing"]))
    ).all()
    cycles = session.scalars(select(Cycle).where(Cycle.started_at >= since)).all()
    llm = session.execute(
        select(LLMCall.task, func.count(), func.sum(LLMCall.cost_usd))
        .where(LLMCall.ts >= since)
        .group_by(LLMCall.task)
    ).all()
    hits = session.execute(
        select(RiskRuleHit.rule, RiskRuleHit.effect, func.count())
        .where(RiskRuleHit.ts >= since)
        .group_by(RiskRuleHit.rule, RiskRuleHit.effect)
    ).all()
    posts = session.scalars(select(XPostOut).where(XPostOut.created_at >= since)).all()
    failed_fetches = session.execute(
        select(FetchRun.source, func.count())
        .where(FetchRun.started_at >= since, FetchRun.ok.is_(False))
        .group_by(FetchRun.source)
    ).all()
    ledgers = {a.track: float(a.equity) for a in session.scalars(select(PaperAccount))}
    suppressed = session.scalars(
        select(SuppressedTrigger).where(SuppressedTrigger.ts >= since)
    ).all()
    with_1d = [s for s in suppressed if s.move_1d_pct is not None]
    with_4h = [s for s in suppressed if s.move_4h_pct is not None]
    suppressed_facts = {
        "count": len(suppressed),
        "avg_abs_move_4h_pct": round(
            sum(abs(s.move_4h_pct or 0) for s in with_4h) / len(with_4h), 2
        )
        if with_4h
        else None,
        "avg_abs_move_1d_pct": round(
            sum(abs(s.move_1d_pct or 0) for s in with_1d) / len(with_1d), 2
        )
        if with_1d
        else None,
    }

    from app.agents.polymarket import coverage
    from app.agents.x_sentiment import mention_volume
    from app.decision.tuning import current_weights, leaderboard

    board = [
        {
            "name": r.name,
            "kind": r.kind,
            "ic_1d": r.ic.get("1d"),
            "ic_4h": r.ic.get("4h"),
            "sample": r.sample,
        }
        for r in leaderboard(session, cfg, now - timedelta(days=90))
    ]
    pm_cov = {a: coverage(session, a, now).status for a in cfg.trading.assets}
    buzz = {}
    for a in cfg.trading.assets:
        mv = mention_volume(session, a, now)
        if mv:
            buzz[a] = {"last_24h": mv[0], "per_day_before": round(mv[1])}
    return {
        "week_ending": now.strftime("%Y-%m-%d"),
        "mode": cfg.trading.live_allowed and "live" or "shadow",
        "pilot_enabled": cfg.pilot.enabled,
        "cycles": {
            "scheduled": sum(1 for c in cycles if c.kind == "scheduled"),
            "triggered": sum(1 for c in cycles if c.kind == "triggered"),
            "failed": sum(1 for c in cycles if c.status != "done"),
            "cost_usd": round(float(sum(c.cost_usd or 0 for c in cycles)), 2),
        },
        "ledgers_equity": ledgers,
        "closed_trades_by_track": by_track,
        "open_positions": [
            {
                "track": p.track,
                "asset": p.asset,
                "direction": p.direction,
                "leverage": float(p.leverage),
            }
            for p in open_rows
        ],
        "pilot_vs_paper": pilot_vs_paper(list(pilot_rows), list(paper_rows)),
        "llm_cost_by_task": {t: {"calls": n, "usd": round(float(c or 0), 2)} for t, n, c in llm},
        "llm_budget_usd": float(cfg.budget.api_usd_per_month),
        "risk_rule_hits": [{"rule": r, "effect": e, "n": n} for r, e, n in hits],
        "posts": {
            "sent": sum(1 for p in posts if p.x_id and not p.dry_run),
            "blocked_by_whitelist": sum(1 for p in posts if not p.whitelist_ok),
            "template_fallbacks": sum(1 for p in posts if p.source == "template" and not p.dry_run),
        },
        "failed_fetches": {s: n for s, n in failed_fetches},
        "suppressed_triggers": suppressed_facts,
        "agent_weights": current_weights(session),
        "leaderboard": board,
        "polymarket_coverage": pm_cov,
        "x_mentions": buzz,
    }


def render(f: dict[str, Any], memo: Memo | None, error: str | None = None) -> str:
    lines = [f"# dorkbot weekly memo, week ending {f['week_ending']}", ""]
    c = f["cycles"]
    lines += [
        f"Mode {f['mode']}, pilot {'on' if f['pilot_enabled'] else 'off'}. "
        f"{c['scheduled']} scheduled + {c['triggered']} triggered cycles, {c['failed']} failed, "
        f"LLM ${c['cost_usd']:.2f} this week (budget ${f['llm_budget_usd']:.0f}/month).",
        "",
        "## Trades, last 7 days",
    ]
    for track, s in f["closed_trades_by_track"].items():
        if track == "live" and not s["closed"]:
            continue  # nothing to say before go-live
        if s["closed"]:
            lines.append(
                f"- {track}: {s['closed']} closed, {s['wins']} won, P&L {s['pnl']:+.2f}, "
                f"PF {s['profit_factor'] if s['profit_factor'] is not None else 'n/a'}, "
                f"avg hold {s['avg_hold_h']} h, exits {s['reasons']}"
            )
        else:
            lines.append(f"- {track}: no closed trades")
    lines.append(f"- equity now: {f['ledgers_equity']}")
    if f["pilot_vs_paper"]:
        lines += ["", "## Pilot vs paper (same decisions, real fills vs simulator)"]
        for r in f["pilot_vs_paper"]:
            head = f"- {r['asset']} {r['direction']}: entry {r['entry_worse_bps']:+.1f} bps worse, "
            tail = (
                f"exit {r['exit_worse_bps']:+.1f} bps worse"
                if r["exit_worse_bps"] is not None
                else "still open"
            )
            lines.append(head + tail)
    lines += ["", "## Agents and models (IC, 1 day, last 90 days)"]
    for r in f["leaderboard"]:
        ic = "n/a" if r["ic_1d"] is None else f"{r['ic_1d']:+.2f}"
        lines.append(f"- {r['kind']} {r['name']}: IC {ic} on {r['sample']} samples")
    lines.append(f"- weights: {f['agent_weights']}")
    lines += ["", "## Plumbing"]
    lines.append(f"- risk rule hits: {f['risk_rule_hits'] or 'none'}")
    lines.append(f"- posts: {f['posts']}")
    lines.append(f"- failed fetches: {f['failed_fetches'] or 'none'}")
    st = f.get("suppressed_triggers") or {}
    if st.get("count"):
        lines.append(
            f"- triggers suppressed by the 2/day cap: {st['count']}, avg |move| after 4h "
            f"{st.get('avg_abs_move_4h_pct')}%, after 1d {st.get('avg_abs_move_1d_pct')}%"
        )
    lines.append(f"- Polymarket: {f['polymarket_coverage']}")
    lines.append(f"- X mentions: {f['x_mentions'] or 'no baseline yet'}")
    lines += ["", "## Proposals for review"]
    if memo is None:
        lines.append(f"(no proposals: {error})")
    else:
        lines += [memo.summary, ""]
        if memo.keep:
            lines += ["Keep as is: " + "; ".join(memo.keep), ""]
        for i, p in enumerate(memo.proposals, 1):
            lines += [
                f"{i}. **{p.title}** ({p.effort})",
                f"   - evidence: {p.evidence}",
                f"   - change: {p.change}",
                f"   - risk: {p.risk}",
            ]
    lines += ["", "Nothing in this memo changes the bot. Reply with the numbers you want applied."]
    return "\n".join(lines) + "\n"


def improvement_prompt(f: dict[str, Any], memo: Memo | None) -> str:
    """A ready-to-paste brief for Claude Code: the owner ticks the proposals to apply."""
    date = f["week_ending"]
    lines = [
        f"# dorkbot improvement brief, week ending {date}",
        "",
        "Paste this into Claude Code in the multi-agent-trading-bot repo after ticking the",
        "proposals to apply. Untouched boxes mean: do not apply, just note it.",
        "",
        f"Context: the weekly memo of {date} (logs/memo-{date}.md on the server, also mailed).",
        "Standing goal: an average of 1% per day on capital, net of every cost, reached by",
        "tuning parameters as the evidence comes in. The hard risk rules (stops, liquidation",
        "guard, drawdown pause, day-loss lock, one-PM-flat, kill switch, order idempotency)",
        "and the non-negotiables in CLAUDE.md are not levers.",
        "",
        "For every ticked proposal: implement the exact change, add or adjust tests, bump the",
        "version of any prompt you touch, record the decision in docs/DECISIONS.md with the",
        "evidence quoted, deploy, and list what changed with before/after numbers where they",
        "exist. Finish with one message summarizing all changes and what the next memo should",
        "measure to judge them.",
        "",
        "## Proposals",
        "",
    ]
    if memo is None or not memo.proposals:
        lines.append("(the memo produced no proposals this week)")
    for i, p in enumerate(memo.proposals if memo else [], 1):
        lines += [
            f"- [ ] {i}. {p.title} ({p.effort})",
            f"  - change: {p.change}",
            f"  - evidence: {p.evidence}",
            f"  - risk: {p.risk}",
        ]
    if memo and memo.keep:
        lines += ["", "## Leave alone (per the memo)", ""] + [f"- {k}" for k in memo.keep]
    lines += [
        "",
        "## Owner notes",
        "",
        "(add anything here: a parameter value you want instead, a proposal to skip, a",
        "question for the next memo)",
        "",
    ]
    return "\n".join(lines)


async def propose(f: dict[str, Any]) -> tuple[Memo | None, str | None]:
    from app.llm import LLMError, complete, load_prompt

    try:
        result = await complete(
            TASK, Memo, prompt=load_prompt("memo"), user_text=json.dumps(f, default=str)
        )
    except (LLMError, ValueError) as exc:  # a late, invalid or overlong answer: facts only
        log.warning("memo proposals unavailable: %s", str(exc)[:200])
        return None, str(exc)[:200]
    return result.parsed, None


async def build(cfg: Config | None = None, *, mail: bool = True) -> Path:
    cfg = cfg or get_config()
    now = datetime.now(UTC)
    with new_session() as session:
        f = facts(session, cfg, now)
    memo, error = await propose(f)
    text = render(f, memo, error)
    brief = improvement_prompt(f, memo)
    path = MEMO_DIR / f"memo-{now:%Y-%m-%d}.md"
    brief_path = MEMO_DIR / f"brief-{now:%Y-%m-%d}.md"
    await asyncio.to_thread(_write, path, text)
    await asyncio.to_thread(_write, brief_path, brief)
    if mail:
        from app.admin import notify

        await asyncio.to_thread(
            notify.send,
            f"dorkbot weekly memo, week ending {now:%Y-%m-%d}",
            text,
            None,
            [(path.name, text), (brief_path.name, brief)],
        )
    return path


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    path = asyncio.run(build(mail="--no-mail" not in args))
    print(path.read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
