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
import re
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
from app.data.fetch import stale_at
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
from app.version import FROZEN_UNTIL, LOGIC_VERSION

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


KNOWN_TRACKS = ("primary", "max", "pilot", "live", "inverse")
CLOSED_BOOKS = ("max", "inverse")  # v11 (2026-10-19): they open nothing new
CONSENSUS_RULE = (
    "a trade opens when two of the three managers call the same direction and none calls "
    "the opposite one; the risk engine sizes it"
)


def decided_log(n: int = 6) -> list[str]:
    """The last logic versions, one line each, from app/version.py: what is already
    decided and must not come back as a proposal (owner 2026-10-26: the brief cited a
    consensus rule and books that were gone)."""
    import app.version as version

    out: list[str] = []
    for line in (version.__doc__ or "").splitlines():
        m = re.match(r"^(\d+)\s+(\d{4}-\d{2}-\d{2})\s+(.*)$", line)
        if m:
            out.append(f"v{m.group(1)} ({m.group(2)}): {m.group(3).strip()}")
        elif out and line.startswith(" ") and len(out[-1]) < 220:
            out[-1] = f"{out[-1]} {line.strip()}"
    return out[:n]


def active_books(session: Session, now: datetime) -> tuple[list[str], list[str]]:
    recent = set(
        session.scalars(
            select(Position.track).where(Position.opened_at >= now - timedelta(days=14)).distinct()
        ).all()
    )
    active = [t for t in KNOWN_TRACKS if t in recent and t not in CLOSED_BOOKS]
    return active, list(CLOSED_BOOKS)


def logic_facts(active: list[str], closed_books: list[str]) -> dict[str, Any]:
    return {
        "version": LOGIC_VERSION,
        "frozen_until": FROZEN_UNTIL,
        "consensus": CONSENSUS_RULE,
        "active_books": active,
        "closed_books": closed_books,
        "note": "closed books open nothing new since v11; their last trades run out. "
        "Judge the bot on the active books only.",
        "already_decided": decided_log(),
    }


def time_exit_facts(closed: list[Position]) -> dict[str, Any]:
    """What the week's time exits left on the table (memo 2026-10-26): the best and
    worst excursion, how far they got toward the target."""
    rows = [p for p in closed if p.close_reason == "time" and p.mfe_pct is not None]
    if not rows:
        return {
            "count": 0,
            "note": "no time exits with excursion data yet (tracked since 2026-10-26)",
        }
    shares = []
    for p in rows:
        if p.entry_price and p.target:
            d = abs(p.target - p.entry_price) / p.entry_price
            if d:
                shares.append(float((p.mfe_pct or 0) / d))
    return {
        "count": len(rows),
        "avg_mfe_pct": round(sum(float(p.mfe_pct or 0) for p in rows) / len(rows) * 100, 2),
        "avg_mae_pct": round(sum(float(p.mae_pct or 0) for p in rows) / len(rows) * 100, 2),
        "avg_target_reached_share": round(sum(shares) / len(shares), 2) if shares else None,
        "avg_exit_price_pct": round(
            sum(float(p.pnl_price_pct or 0) for p in rows) / len(rows) * 100, 2
        ),
        "reached_half_of_target": sum(1 for x in shares if x >= 0.5),
    }


def facts(session: Session, cfg: Config, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = now - WEEK
    closed = session.scalars(
        select(Position).where(Position.status == "closed", Position.closed_at >= since)
    ).all()
    # a book is active when it opened a trade in the last two weeks; the max and inverse
    # books stopped opening at v11 and must not be judged as if they still traded
    active, closed_books = active_books(session, now)
    by_track = {t: _track_stats([p for p in closed if p.track == t]).__dict__ for t in active}
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
    full_cycles = [c for c in cycles if c.kind != "light"]
    stale_counts: Counter[str] = Counter()
    stale_cycles = 0
    for c in full_cycles:
        stale = stale_at(session, c.started_at)
        if stale:
            stale_cycles += 1
            stale_counts.update(stale)
    return {
        "week_ending": now.strftime("%Y-%m-%d"),
        "logic": logic_facts(active, closed_books),
        "stale_input_cycles": {
            "full_cycles": len(full_cycles),
            "on_stale_input": stale_cycles,
            "by_source": dict(stale_counts),
        },
        "time_exits": time_exit_facts([p for p in closed if p.track in active]),
        "mode": cfg.trading.live_allowed and "live" or "shadow",
        "pilot_enabled": cfg.pilot.enabled,
        "aggressive_mode": aggressive_fact(session),
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
        "sleeves": sleeves_facts(session, cfg, now, since),
        "agent_weights": current_weights(session),
        "leaderboard": board,
        "polymarket_coverage": pm_cov,
        "x_mentions": buzz,
        "optima": optima_facts(session),
        # v11 (2026-10-19): the charts-first, solo-call and three-seat comparisons are
        # decided and no longer computed; their rows stay in the database for the record.
    }


def charts_first_facts(session: Session, cfg: Config, now: datetime) -> list[dict[str, Any]]:
    """The logged charts-first score against the real one (owner 2026-10-10)."""
    from app.decision.charts_first import compare

    return compare(session, cfg, now - timedelta(days=90))


def solo_facts(session: Session, cfg: Config, now: datetime) -> dict[str, Any]:
    """What followed when one manager called and the other stayed flat (owner 2026-10-11)."""
    from app.decision.solo_calls import compare

    return compare(session, cfg, now - timedelta(days=90))


def three_seat_facts(session: Session, cfg: Config, now: datetime) -> dict[str, Any]:
    """Two of two, two of three and a score threshold on the same decisions (logged)."""
    from app.decision.three_seat import compare

    return compare(session, cfg, now - timedelta(days=90))


def aggressive_fact(session: Session) -> dict[str, Any]:
    """Whether the owner's aggressive-mode switch is on, and since when."""
    from app.db.models import RiskState

    state = session.get(RiskState, 1)
    on = bool(state and state.aggressive)
    since = state.aggressive_since if state and on else None
    return {"on": on, "since": since.strftime("%Y-%m-%d %H:%M UTC") if since else None}


def optima_facts(session: Session) -> dict[str, Any] | None:
    """Optima's latest report (owner 2026-10-10): a separate section of the memo, never
    merged into Coach's proposals. None when there is no run from this week."""
    from app.agents.optima import latest

    run = latest(session)
    if run is None:
        return None
    return {
        "as_of": run.ts.strftime("%Y-%m-%d %H:%M UTC"),
        "model": run.model,
        "report": run.report,
        "error": run.error,
        "budget": (run.facts or {}).get("budget"),
    }


def sleeves_facts(session: Session, cfg: Config, now: datetime, since: datetime) -> dict[str, Any]:
    """Per sleeve: its account view, its closed trades this week and its costs."""
    if not cfg.sleeves:
        return {}
    from app.costs_sleeves import costs_by_sleeve
    from app.risk.exposure import betas
    from app.risk.sleeves import sleeve_account

    b = betas(session, cfg, now)
    costs = {c.sleeve: c for c in costs_by_sleeve(session, cfg, since)}
    closed = session.scalars(
        select(Position).where(
            Position.status == "closed", Position.closed_at >= since, Position.track == "primary"
        )
    ).all()
    out: dict[str, Any] = {}
    for name, s in cfg.sleeves.items():
        sa = sleeve_account(session, cfg, name, "primary", now, b, 1)
        mine = [p for p in closed if p.asset in s.assets]
        c = costs.get(name)
        out[name] = {
            "assets": s.assets,
            "capital_fraction": float(s.capital_fraction),
            "risk_per_trade": float(s.risk_per_trade),
            "leverage_max": s.leverage_max,
            "equity": float(sa.equity),
            "pnl_since_start": float(sa.equity - sa.capital),
            "net_beta_x_equity": float(sa.net_beta_exposure / sa.equity) if sa.equity else None,
            "closed_this_week": _track_stats(mine).__dict__,
            "llm_usd_month": float(c.llm_usd) if c else 0.0,
            "x_usd_month": float(c.x_usd) if c else 0.0,
        }
    shared = costs.get("shared")
    out["shared_costs"] = {
        "llm_usd_month": float(shared.llm_usd) if shared else 0.0,
        "x_usd_month": float(shared.x_usd) if shared else 0.0,
    }
    return out


def render(f: dict[str, Any], memo: Memo | None, error: str | None = None) -> str:
    lines = [f"# dorkbot weekly memo, week ending {f['week_ending']}", ""]
    lg = f.get("logic") or {}
    if lg:
        lines += [
            f"Logic v{lg['version']}, untouched until {lg['frozen_until']}. "
            f"Rule: {lg['consensus']}. "
            f"Active books: {', '.join(lg['active_books']) or 'none'}"
            + (
                f"; closed books (no new trades): {', '.join(lg['closed_books'])}"
                if lg["closed_books"]
                else ""
            )
            + ".",
            "",
        ]
    c = f["cycles"]
    lines += [
        f"Mode {f['mode']}, pilot {'on' if f['pilot_enabled'] else 'off'}"
        + (
            f", AGGRESSIVE MODE ON since {f['aggressive_mode']['since']}"
            if (f.get("aggressive_mode") or {}).get("on")
            else ""
        )
        + ". "
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
    for name, sv in (f.get("sleeves") or {}).items():
        if name == "shared_costs":
            continue
        cw = sv["closed_this_week"]
        lines.append(
            f"- sleeve {name} ({', '.join(sv['assets'])}): equity {sv['equity']:,.2f}, "
            f"P&L since start {sv['pnl_since_start']:+,.2f}, this week {cw['closed']} closed / "
            f"{cw['wins']} won, cost this month LLM ${sv['llm_usd_month']:.2f} "
            f"+ X ${sv['x_usd_month']:.2f}"
        )
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
    te = f.get("time_exits") or {}
    if te.get("count"):
        lines += ["", "## Time exits (what the clock closed, and how close they came)"]
        lines.append(
            f"- {te['count']} time exits: best on average {te['avg_mfe_pct']:+.2f}% "
            f"({te['reached_half_of_target']} got at least halfway to the target, "
            f"average {te['avg_target_reached_share']:.0%} of the way), worst "
            f"{te['avg_mae_pct']:+.2f}%, closed at {te['avg_exit_price_pct']:+.2f}% on price"
        )
    lines += ["", "## Plumbing"]
    lines.append(f"- risk rule hits: {f['risk_rule_hits'] or 'none'}")
    lines.append(f"- posts: {f['posts']}")
    lines.append(f"- failed fetches: {f['failed_fetches'] or 'none'}")
    si = f.get("stale_input_cycles") or {}
    if si:
        lines.append(
            f"- cycles on stale input: {si['on_stale_input']} of {si['full_cycles']}"
            + (f" ({si['by_source']})" if si["by_source"] else "")
        )
    st = f.get("suppressed_triggers") or {}
    if st.get("count"):
        lines.append(
            f"- triggers suppressed by the 2/day cap: {st['count']}, avg |move| after 4h "
            f"{st.get('avg_abs_move_4h_pct')}%, after 1d {st.get('avg_abs_move_1d_pct')}%"
        )
    lines.append(f"- Polymarket: {f['polymarket_coverage']}")
    lines.append(f"- X mentions: {f['x_mentions'] or 'no baseline yet'}")
    lines += optima_section(f.get("optima"))
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


def optima_section(o: dict[str, Any] | None) -> list[str]:
    """Optima's findings and proposals as their own section (cost, execution, budget)."""
    if not o:
        return ["", "## Optima (cost, execution, budget)", "(no Optima run this week)"]
    lines = ["", f"## Optima (cost, execution, budget), as of {o['as_of']}"]
    b = o.get("budget") or {}
    if b:
        lines.append(
            f"- API spend: ${b.get('month_to_date_usd', 0):.2f} month to date, "
            f"${b.get('daily_rate_usd', 0):.2f}/day lately, forecast "
            f"${b.get('forecast_usd', 0):.2f} of ${b.get('budget_usd', 0):.0f}"
        )
    r = o.get("report")
    if not r:
        lines.append(f"(no findings: {o.get('error')})")
        return lines
    lines += [r["summary"], ""]
    for fnd in r.get("findings", []):
        lines.append(f"- [{fnd['severity']}] {fnd['area']}: **{fnd['title']}** {fnd['detail']}")
        for ev in fnd.get("evidence", []):
            lines.append(f"  - {ev}")
    if r.get("proposals"):
        lines.append("")
        lines.append("Optima's proposals (advisory; nothing is applied by itself):")
        for i, pr in enumerate(r["proposals"], 1):
            delta = pr.get("cost_delta_usd_month")
            money = f", {delta:+.2f} $/month" if delta is not None else ""
            lines += [
                f"{i}. **{pr['title']}** (needs: {pr['needs']}{money})",
                f"   - change: {pr['change']}",
                f"   - expected: {pr['expected_effect']}",
                f"   - risk: {pr['risk']}",
            ]
    return lines


def improvement_prompt(f: dict[str, Any], memo: Memo | None) -> str:
    """A ready-to-paste brief for Claude Code: the owner ticks the proposals to apply."""
    date = f["week_ending"]
    lg = f.get("logic") or {
        "version": LOGIC_VERSION,
        "frozen_until": FROZEN_UNTIL,
        "consensus": CONSENSUS_RULE,
        "active_books": [],
        "closed_books": [],
        "already_decided": [],
    }
    lines = [
        f"# dorkbot improvement brief, week ending {date}",
        "",
        "Paste this into Claude Code in the multi-agent-trading-bot repo after ticking the",
        "proposals to apply. Untouched boxes mean: do not apply, just note it.",
        "",
        f"Context: the weekly memo of {date} (logs/memo-{date}.md on the server, also mailed).",
        f"Logic v{lg['version']}, untouched until {lg['frozen_until']}. Rule: {lg['consensus']}.",
        f"Active books: {', '.join(lg['active_books']) or 'none'}"
        + (
            f". Closed books (open nothing new): {', '.join(lg['closed_books'])}."
            if lg["closed_books"]
            else "."
        ),
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
    if lg.get("already_decided"):
        lines += ["", "## Already decided (not up for proposal)", ""] + [
            f"- {d}" for d in lg["already_decided"]
        ]
    o = f.get("optima") or {}
    r = o.get("report") or {}
    if r.get("proposals"):
        lines += ["", "## Optima's proposals (cost, execution, budget)", ""]
        for i, pr in enumerate(r["proposals"], 1):
            lines += [
                f"- [ ] O{i}. {pr['title']} (needs: {pr['needs']})",
                f"  - change: {pr['change']}",
                f"  - expected: {pr['expected_effect']}",
                f"  - risk: {pr['risk']}",
            ]
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
