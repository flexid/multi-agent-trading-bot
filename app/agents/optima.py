"""Optima: the read-only cost and execution adviser (owner 2026-10-10), crew member 15.

Weekly, before Coach's Monday memo, it computes in code:
- cost versus value per agent and per model: cost this month, IC (1 day, last 90 days),
  IC per dollar; the main labeller against its shadow (Jev vs Sonnet) and the PM models
  against their alt seats (Fable vs Astra, Opus, ...);
- execution quality over the last 30 days: planned versus actual entry, slippage in
  basis points, missed post-only entries, fee drag per sleeve, depth usage;
- the month-end API spend forecast against ``budget.api_usd_per_month``.

claude-opus-5-5 then writes findings and proposals into a validated schema. Optima never
changes config, models or code; its report is stored (``optima_runs``), shown in the
admin and handed to Coach as a separate section of the memo. Costs never reach the site.
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Config, get_config
from app.db.models import (
    DecisionRecord,
    LLMCall,
    OptimaRun,
    OrderBookSnapshot,
    OrderRecord,
    Position,
    XPostRecord,
)
from app.db.session import new_session

log = logging.getLogger(__name__)

AGENT = "optima"
TASK = "optima"
EXECUTION_DAYS = 30
IC_DAYS = 90
X_READ_USD = Decimal("0.005")
X_POST_USD = Decimal("0.015")
# LLM tasks that belong to each analysis agent (cost attribution)
AGENT_TASKS: dict[str, tuple[str, ...]] = {
    "indicators": ("indicators",),
    "chart_patterns": ("chart_patterns",),
    "polymarket": ("polymarket",),
    "macro": ("macro",),
    "x_sentiment": ("x_sentiment",),
    "catalysts": ("catalysts",),
}


class Area(StrEnum):
    COST_VALUE = "cost_value"
    EXECUTION = "execution"
    BUDGET = "budget"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Needs(StrEnum):
    OWNER = "owner"  # a parameter or spend decision only the owner makes
    REVIEW = "review"  # a decision-logic change: waits for the review date
    NONE = "none"  # an ops or reporting change


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area: Area
    severity: Severity
    title: str = Field(max_length=80)
    detail: str = Field(max_length=400)
    evidence: list[str] = Field(max_length=4)  # the facts quoted with their numbers


class OptimaProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=80)
    change: str = Field(max_length=400)  # the concrete change, for the owner to tick
    expected_effect: str = Field(max_length=300)
    cost_delta_usd_month: float | None = None  # negative = saves money
    risk: str = Field(max_length=300)
    needs: Needs


class OptimaReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(max_length=1500)
    findings: list[Finding] = Field(max_length=10)
    proposals: list[OptimaProposal] = Field(max_length=6)
    budget_on_track: bool


# --- pure helpers -------------------------------------------------------------------


def ic_per_dollar(ic: float | None, usd_month: float) -> float | None:
    """IC per dollar of monthly cost; None without an IC. A free reading divides by one
    cent so it ranks first rather than infinitely."""
    if ic is None:
        return None
    return round(ic / max(usd_month, 0.01), 4)


def slippage_bps(entry: Decimal, planned: Decimal, direction: str) -> float:
    """Positive = filled worse than planned (paid more on a long, got less on a short)."""
    sign = 1 if direction == "long" else -1
    return round(float((entry / planned - 1) * 10_000) * sign, 1)


def forecast_month(mtd_usd: float, daily_rate_usd: float, now: datetime) -> dict[str, Any]:
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_left = days_in_month - now.day
    return {
        "month_to_date_usd": round(mtd_usd, 2),
        "daily_rate_usd": round(daily_rate_usd, 2),
        "days_left": days_left,
        "forecast_usd": round(mtd_usd + daily_rate_usd * days_left, 2),
    }


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 1)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


# --- facts (code only) --------------------------------------------------------------


def cost_value_facts(session: Session, cfg: Config, now: datetime) -> dict[str, Any]:
    from app.decision.tuning import current_weights, score_agents, score_models

    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    since_ic = now - timedelta(days=IC_DAYS)
    by_task_model = session.execute(
        select(
            LLMCall.task,
            LLMCall.model,
            LLMCall.provider,
            func.count(),
            func.coalesce(func.sum(LLMCall.cost_usd), 0),
            func.coalesce(func.avg(LLMCall.latency_ms), 0),
        )
        .where(LLMCall.ts >= month)
        .group_by(LLMCall.task, LLMCall.model, LLMCall.provider)
    ).all()
    main_ic = {s.name: s for s in score_agents(session, cfg, since_ic)}
    alt_ic = {s.name: s for s in score_agents(session, cfg, since_ic, variant="alt")}
    weights = current_weights(session)
    shadow = cfg.catalysts.shadow_model
    agents = []
    for agent, tasks in AGENT_TASKS.items():
        rows = [r for r in by_task_model if r[0] in tasks]
        main_rows = [r for r in rows if r[1] != shadow or agent not in ("catalysts", "x_sentiment")]
        shadow_rows = [r for r in rows if r not in main_rows]
        usd = float(sum(Decimal(str(r[4])) for r in main_rows))
        ic = main_ic.get(agent)
        entry: dict[str, Any] = {
            "agent": agent,
            "weight": weights.get(agent),
            "usd_month": round(usd, 2),
            "calls_month": sum(int(r[3]) for r in main_rows),
            "models": sorted({str(r[1]) for r in main_rows}),
            "ic_1d": ic.ic.get("1d") if ic else None,
            "ic_4h": ic.ic.get("4h") if ic else None,
            "sample": ic.sample if ic else 0,
            "ic_per_dollar": ic_per_dollar(ic.ic.get("1d") if ic else None, usd),
        }
        if shadow_rows or agent in alt_ic:
            s_usd = float(sum(Decimal(str(r[4])) for r in shadow_rows))
            alt = alt_ic.get(agent)
            entry["shadow"] = {
                "model": shadow,
                "usd_month": round(s_usd, 2),
                "calls_month": sum(int(r[3]) for r in shadow_rows),
                "ic_1d": alt.ic.get("1d") if alt else None,
                "sample": alt.sample if alt else 0,
                "ic_per_dollar": ic_per_dollar(alt.ic.get("1d") if alt else None, s_usd),
            }
        agents.append(entry)
    models: dict[str, dict[str, Any]] = {}
    for task, model, provider, n, usd, latency in by_task_model:
        m = models.setdefault(
            str(model),
            {
                "model": str(model),
                "provider": str(provider),
                "usd_month": 0.0,
                "calls": 0,
                "tasks": set(),
                "latency_ms": [],
            },
        )
        m["usd_month"] = round(m["usd_month"] + float(usd), 2)
        m["calls"] += int(n)
        m["tasks"].add(str(task))
        m["latency_ms"].append(float(latency))
    for m in models.values():
        m["tasks"] = sorted(m["tasks"])
        m["latency_ms"] = int(sum(m["latency_ms"]) / len(m["latency_ms"]))
    pm_scores = []
    for s in score_models(session, cfg, since_ic):
        model_name = s.name.split(" (")[0]
        cost = models.get(model_name, {}).get("usd_month", 0.0)
        pm_scores.append(
            {
                "seat": s.name,
                "ic_1d": s.ic.get("1d"),
                "sample": s.sample,
                "model_usd_month": cost,
                "ic_per_dollar": ic_per_dollar(s.ic.get("1d"), cost),
            }
        )
    return {
        "agents": agents,
        "models": sorted(models.values(), key=lambda m: -m["usd_month"]),
        "pm_seats": pm_scores,
        "model_config": {
            "main": {"pm_1": cfg.models.pm_1, "pm_2": cfg.models.pm_2},
            "alt": (
                {"pm_1": cfg.models.shadow_alt.pm_1, "pm_2": cfg.models.shadow_alt.pm_2}
                if cfg.models.shadow_alt
                else None
            ),
            "catalysts": cfg.models.catalysts,
            "catalysts_shadow": shadow,
        },
    }


def _depth_at(session: Session, symbol: str, at: datetime) -> Decimal | None:
    row = session.execute(
        select(OrderBookSnapshot.depth_bid_2pct, OrderBookSnapshot.depth_ask_2pct)
        .where(OrderBookSnapshot.symbol == symbol, OrderBookSnapshot.ts <= at)
        .order_by(OrderBookSnapshot.ts.desc())
        .limit(1)
    ).first()
    return (row[0] + row[1]) if row else None


def execution_facts(session: Session, cfg: Config, now: datetime) -> dict[str, Any]:
    since = now - timedelta(days=EXECUTION_DAYS)
    positions = session.scalars(
        select(Position).where(
            Position.opened_at >= since, Position.status.in_(["open", "closing", "closed"])
        )
    ).all()
    decisions = {
        d.id: d
        for d in session.scalars(
            select(DecisionRecord).where(DecisionRecord.ts >= since - timedelta(days=1))
        ).all()
    }
    by_group: dict[str, dict[str, list[float]]] = {}
    for p in positions:
        d = decisions.get(p.decision_id or -1)
        planned = (
            Decimal(str(d.proposal["entry"]))
            if d and d.proposal and d.proposal.get("entry")
            else None
        )
        key = f"{p.mode}/{p.sleeve or 'none'}"
        g = by_group.setdefault(
            key, {"slip": [], "fee_bps": [], "fee_share": [], "depth_use": [], "n": []}
        )
        g["n"].append(1.0)
        if planned and p.entry_price:
            g["slip"].append(slippage_bps(p.entry_price, planned, p.direction))
        costs = float((p.fees or 0) + (p.interest or 0))
        if p.notional:
            g["fee_bps"].append(costs / float(p.notional) * 10_000)
        if p.status == "closed" and p.pnl is not None:
            gross = abs(float(p.pnl) + costs)
            if gross > 0:
                g["fee_share"].append(min(5.0, costs / gross))
        if p.opened_at:
            depth = _depth_at(session, p.symbol, p.opened_at)
            if depth and depth > 0:
                g["depth_use"].append(float(p.notional / depth))
    groups = {
        key: {
            "positions": int(sum(g["n"])),
            "entry_slippage_bps_mean": _mean(g["slip"]),
            "entry_slippage_bps_p90": _p90(g["slip"]),
            "fee_drag_bps_of_notional": _mean(g["fee_bps"]),
            "costs_share_of_gross_pnl": _mean(g["fee_share"]),
            "depth_usage_mean": _mean(g["depth_use"]),  # notional / ±2% book at entry
            "depth_usage_max": round(max(g["depth_use"]), 3) if g["depth_use"] else None,
        }
        for key, g in by_group.items()
    }
    missed = session.execute(
        select(DecisionRecord.action, func.count())
        .where(DecisionRecord.ts >= since, DecisionRecord.action.in_(["rejected", "borrow_failed"]))
        .group_by(DecisionRecord.action)
    ).all()
    opened = session.execute(
        select(func.count())
        .select_from(DecisionRecord)
        .where(DecisionRecord.ts >= since, DecisionRecord.action == "open")
    ).scalar_one()
    entry_orders = session.execute(
        select(OrderRecord.status, func.count())
        .where(OrderRecord.created_at >= since, OrderRecord.purpose == "entry")
        .group_by(OrderRecord.status)
    ).all()
    from app.memo import pilot_vs_paper

    real = [p for p in positions if p.mode in ("pilot", "live")]
    paper = [p for p in positions if p.track == "primary"]
    twins = pilot_vs_paper(real, paper)
    return {
        "window_days": EXECUTION_DAYS,
        "by_mode_and_sleeve": groups,
        "entries_opened": int(opened),
        "entries_missed": {a: int(n) for a, n in missed},  # post-only not filled, borrow failed
        "entry_orders_by_status": {s: int(n) for s, n in entry_orders},
        "real_vs_simulator": {
            "pairs": len(twins),
            "entry_worse_bps_mean": _mean([t["entry_worse_bps"] for t in twins]),
            "exit_worse_bps_mean": _mean(
                [t["exit_worse_bps"] for t in twins if t["exit_worse_bps"] is not None]
            ),
        },
    }


def budget_facts(session: Session, cfg: Config, now: datetime) -> dict[str, Any]:
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week = now - timedelta(days=7)

    def spend(since: datetime) -> float:
        llm = session.execute(
            select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(LLMCall.ts >= since)
        ).scalar_one()
        reads = session.execute(
            select(func.count()).select_from(XPostRecord).where(XPostRecord.fetched_at >= since)
        ).scalar_one()
        return float(Decimal(str(llm)) + Decimal(reads) * X_READ_USD)

    mtd = spend(month)
    last_week = spend(week)
    out = forecast_month(mtd, last_week / 7, now)
    budget = float(cfg.budget.api_usd_per_month)
    out.update(
        {
            "budget_usd": budget,
            "forecast_share_of_budget": round(out["forecast_usd"] / budget, 2) if budget else None,
            "by_provider_month": {
                str(p): round(float(u), 2)
                for p, u in session.execute(
                    select(LLMCall.provider, func.coalesce(func.sum(LLMCall.cost_usd), 0))
                    .where(LLMCall.ts >= month)
                    .group_by(LLMCall.provider)
                ).all()
            },
        }
    )
    return out


def facts(session: Session, cfg: Config, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    return {
        "as_of": now.strftime("%Y-%m-%d %H:%M UTC"),
        "cost_value": cost_value_facts(session, cfg, now),
        "execution": execution_facts(session, cfg, now),
        "budget": budget_facts(session, cfg, now),
    }


# --- the adviser --------------------------------------------------------------------


async def advise(f: dict[str, Any]) -> tuple[OptimaReport | None, str | None, Decimal, str]:
    from app.llm import LLMError, complete, load_prompt

    try:
        result = await complete(
            TASK, OptimaReport, prompt=load_prompt("optima"), user_text=json.dumps(f, default=str)
        )
    except (LLMError, ValueError) as exc:  # late, invalid or overlong: facts only
        log.warning("optima report unavailable: %s", str(exc)[:200])
        return None, str(exc)[:200], Decimal(0), ""
    return result.parsed, None, result.cost_usd, result.model


async def build(cfg: Config | None = None) -> OptimaRun:
    """Compute the facts, ask the adviser, store the run. Never touches config or code."""
    cfg = cfg or get_config()
    now = datetime.now(UTC)
    with new_session() as session:
        f = facts(session, cfg, now)
    report, error, cost, model = await advise(f)
    with new_session() as session:
        run = OptimaRun(
            ts=now,
            model=model or cfg.models.optima,
            cost_usd=cost,
            facts=json.loads(json.dumps(f, default=str)),
            report=json.loads(report.model_dump_json()) if report else None,
            error=error,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
    return run


def latest(session: Session, max_age: timedelta = timedelta(days=8)) -> OptimaRun | None:
    row = session.scalars(select(OptimaRun).order_by(OptimaRun.ts.desc()).limit(1)).first()
    if row is None or row.ts < datetime.now(UTC) - max_age:
        return None
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--facts-only", action="store_true", help="print the facts, no model")
    args = parser.parse_args(argv)
    if args.facts_only:
        with new_session() as session:
            print(json.dumps(facts(session, get_config()), indent=1, default=str))
        return 0
    run = asyncio.run(build())
    print(json.dumps(run.report or {"error": run.error}, indent=1))
    print(f"optima run {run.id}: {run.model}, ${run.cost_usd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
