"""One decision cycle (SPEC §7): agents → evidence pack → two PMs → consensus → log.

    python -m app.decision.cycle [--no-x-fetch] [--no-vision] [--no-alt] [--trigger TEXT]

Everything is written to Postgres as it happens: agent outputs, both PMs' proposals,
the decision per asset and the cycle's model cost. The risk engine (M5) reads the
decisions; this module never sizes or places anything.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents import run_catalysts, run_chart, run_indicators, run_macro, run_polymarket, run_x
from app.agents.indicators_summary import summarize
from app.agents.schema import AgentOutput
from app.config import Config, get_config
from app.db.models import AgentOutputRecord, Cycle, DecisionRecord, LLMCall, PMProposalRecord
from app.db.session import new_session
from app.decision.consensus import Consensus, consensus, weights_for
from app.decision.evidence import build_pack
from app.decision.pm import Proposal, ask_both
from app.decision.tuning import current_weights
from app.llm import load_prompt
from app.risk.apply import apply_risk
from app.version import LOGIC_VERSION

log = logging.getLogger("cycle")


async def run_agents(
    cfg: Config, cycle_id: int, *, x_fetch: bool, vision: bool
) -> tuple[dict[str, list[AgentOutput]], str | None, dict[str, float]]:
    """All five agents; a failing agent is simply absent (and therefore not valid)."""
    by_asset: dict[str, list[AgentOutput]] = {a: [] for a in cfg.trading.assets}
    regime: str | None = None
    couplings: dict[str, float] = {}

    async def indicators() -> list[AgentOutput]:
        prompt = load_prompt("indicators_summary")
        outs = await asyncio.to_thread(run_indicators.run, cfg)
        return list(await asyncio.gather(*(summarize(o, prompt, cycle_id) for o in outs)))

    async def macro() -> list[AgentOutput]:
        nonlocal regime
        outs = await run_macro.run(cfg, cycle_id)
        regime = outs[0].evidence[0].split(" ")[1] if outs else None
        for o in outs:
            try:
                couplings[o.asset] = float(o.evidence[0].split("coupling ")[1])
            except (IndexError, ValueError):
                couplings[o.asset] = 0.0
        return outs

    tasks = {
        "indicators": indicators(),
        "polymarket": asyncio.to_thread(run_polymarket.run, cfg),
        "macro": macro(),
        "x_sentiment": run_x.run(cfg, fetch=x_fetch, cycle_id=cycle_id),
        "chart_patterns": run_chart.run(cfg, vision=vision, cycle_id=cycle_id),
        "catalysts": run_catalysts.run(cfg, fetch=x_fetch, cycle_id=cycle_id),  # alts only
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for name, result in zip(tasks, results, strict=True):
        if isinstance(result, BaseException):
            log.warning("agent %s failed: %s", name, result)
            continue
        for out in result:
            by_asset[out.asset].append(out)
    return by_asset, regime, couplings


def store_outputs(
    cycle_id: int, by_asset: dict[str, list[AgentOutput]], variant: str = "main"
) -> None:
    with new_session() as session:
        for outs in by_asset.values():
            for o in outs:
                session.add(
                    AgentOutputRecord(
                        cycle_id=cycle_id,
                        agent=o.agent,
                        asset=o.asset,
                        variant=variant,
                        score=o.score,
                        confidence=o.confidence,
                        horizon=o.horizon.value,
                        evidence=o.evidence,
                        risk_flags=o.risk_flags,
                        data_age_min=o.data_age_min,
                        valid=o.valid,
                        computed_at=o.computed_at,
                        components=o.components or None,
                    )
                )
        session.commit()


def store_shadow_outputs(cfg: Config, cycle_id: int) -> None:
    """While not live, the alt-sleeve readings scored from the shadow labeller (Sonnet
    beside Jev) are stored as variant ``alt`` so both get an IC (owner 2026-10-10)."""
    try:
        shadow = run_catalysts.run_shadow(cfg)
        shadow += run_x.run_shadow(cfg)
    except Exception as exc:  # a comparison must never fail the cycle
        log.warning("shadow outputs failed: %s", exc)
        return
    if shadow:
        store_outputs(cycle_id, _group(shadow), "alt")


def _group(outs: list[AgentOutput]) -> dict[str, list[AgentOutput]]:
    grouped: dict[str, list[AgentOutput]] = {}
    for o in outs:
        grouped.setdefault(o.asset, []).append(o)
    return grouped


def _dec(value: float | None) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def store_proposals(
    cycle_id: int, pm: str, variant: str, model: str, proposals: dict[str, Proposal]
) -> None:
    with new_session() as session:
        for p in proposals.values():
            session.add(
                PMProposalRecord(
                    cycle_id=cycle_id,
                    pm=pm,
                    variant=variant,
                    model=model,
                    asset=p.asset,
                    direction=p.direction.value,
                    entry_low=_dec(p.entry_low),
                    entry_high=_dec(p.entry_high),
                    stop=_dec(p.stop),
                    target=_dec(p.target),
                    max_hold_hours=p.max_hold_hours,
                    score=p.score,
                    conviction=p.conviction,
                    reasons=p.reasons,
                    weighted_up=p.weighted_up,
                    weighted_down=p.weighted_down,
                )
            )
        session.commit()


def store_decisions(
    cycle_id: int,
    now: datetime,
    results: dict[str, Consensus],
    spots: dict[str, float],
    pm1: dict[str, Proposal],
    pm2: dict[str, Proposal],
) -> None:
    with new_session() as session:
        for asset, c in results.items():
            proposal: dict[str, Any] | None = (
                c.proposal.model_dump(mode="json") if c.proposal else None
            )
            session.add(
                DecisionRecord(
                    cycle_id=cycle_id,
                    asset=asset,
                    ts=now,
                    spot=_dec(spots.get(asset)) if spots.get(asset) else None,
                    valid_agents=c.valid_agents,
                    formula_score=c.formula_score,
                    pm1_direction=pm1[asset].direction.value if asset in pm1 else None,
                    pm2_direction=pm2[asset].direction.value if asset in pm2 else None,
                    agreement=c.agreement.value,
                    consensus_score=c.consensus_score,
                    direction=c.direction.value,
                    conviction=c.conviction,
                    proposal=proposal,
                    reason=c.reason,
                )
            )
        session.commit()


def cycle_cost(cycle_id: int) -> Decimal:
    with new_session() as session:
        total = session.execute(
            select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(LLMCall.cycle_id == cycle_id)
        ).scalar_one()
    return Decimal(str(total))


STALE_CYCLE_MIN = 15  # a cycle takes ~2.5 min; older "running" rows belong to a dead process


def abort_stale_cycles(session: Session, now: datetime | None = None) -> int:
    """Cycles left "running" by a restart (deploy, crash) are marked aborted, so the log
    and the memo do not show an empty cycle as still in progress."""
    now = now or datetime.now(UTC)
    rows = session.scalars(
        select(Cycle).where(
            Cycle.status == "running", Cycle.started_at < now - timedelta(minutes=STALE_CYCLE_MIN)
        )
    ).all()
    for c in rows:
        c.status, c.finished_at = "aborted", now
        c.error = "the scheduler stopped during this cycle (restart); no decisions were made"
    session.commit()
    return len(rows)


def asset_overrides(cfg: Config, asset: str) -> dict[str, float]:
    """Agent-weight overrides for an asset: its sleeve's first, then the per-asset ones."""
    sleeve = cfg.sleeve_cfg(asset)
    return {**(sleeve.weights if sleeve else {}), **cfg.agents.weights_by_asset.get(asset, {})}


async def run_cycle(
    cfg: Config,
    *,
    kind: str = "scheduled",
    trigger: str | None = None,
    x_fetch: bool = True,
    vision: bool = True,
    alt: bool = True,
) -> dict[str, Consensus]:
    now = datetime.now(UTC)
    mode = "live" if cfg.trading.live_allowed else "shadow"  # the go-live checker (M9) refines this
    with new_session() as session:
        abort_stale_cycles(session, now)
        cycle = Cycle(
            started_at=now,
            kind=kind,
            trigger=trigger,
            mode=mode,
            status="running",
            logic_version=LOGIC_VERSION,
        )
        session.add(cycle)
        session.commit()
        cycle_id = cycle.id
    results: dict[str, Consensus] = {}
    try:
        by_asset, regime, couplings = await run_agents(
            cfg, cycle_id, x_fetch=x_fetch, vision=vision
        )
        store_outputs(cycle_id, by_asset)
        store_shadow_outputs(cfg, cycle_id)
        symbols = {a: cfg.symbol(a) for a in cfg.trading.assets}
        with new_session() as session:
            pack = build_pack(
                session,
                mode=mode,
                outputs=by_asset,
                symbols=symbols,
                macro_regime=regime,
                couplings=couplings,
                now=now,
            )
        spots = {a.asset: a.spot for a in pack.assets}
        prompt = load_prompt("pm")
        models = {"pm_1": cfg.models.pm_1, "pm_2": cfg.models.pm_2}
        answers = await ask_both(pack, prompt, models=models, cycle_id=cycle_id)
        for pm, (props, _, err) in answers.items():
            if err:
                log.warning("%s failed: %s", pm, err)
            store_proposals(cycle_id, pm, "main", models[pm], props)
        if alt and cfg.models.shadow_alt is not None:
            alt_models = {"pm_1": cfg.models.shadow_alt.pm_1, "pm_2": cfg.models.shadow_alt.pm_2}
            alt_answers = await ask_both(pack, prompt, models=alt_models, cycle_id=cycle_id)
            for pm, (props, _, _) in alt_answers.items():
                store_proposals(cycle_id, pm, "alt", alt_models[pm], props)
        pm1, pm2 = answers["pm_1"][0], answers["pm_2"][0]
        with new_session() as session:
            weights = current_weights(session)
        results = {
            asset: consensus(
                by_asset[asset],
                couplings.get(asset),
                pm1.get(asset),
                pm2.get(asset),
                weights_for(weights, asset_overrides(cfg, asset)),
            )
            for asset in cfg.trading.assets
        }
        store_decisions(cycle_id, now, results, spots, pm1, pm2)
        with new_session() as session:
            assessments = apply_risk(session, cfg, cycle_id, mode, now)
        for asset, a in assessments.items():
            caps = ", ".join(f"{h.rule}={h.detail}" for h in a.hits if h.effect in ("cap", "block"))
            if a.plan:
                log.info(
                    "%s: %s %.1fx notional %s margin %s%s",
                    asset,
                    a.plan.direction.value,
                    a.plan.leverage,
                    a.plan.notional,
                    a.plan.margin,
                    f" [{caps}]" if caps else "",
                )
            else:
                log.info("%s: no trade (%s)", asset, caps or "no consensus")
        status, error = "done", None
    except Exception as exc:
        log.exception("cycle %d failed", cycle_id)
        status, error = "failed", f"{type(exc).__name__}: {exc}"[:2000]
    with new_session() as session:
        done = session.get(Cycle, cycle_id)
        assert done is not None
        done.finished_at = datetime.now(UTC)
        done.status, done.error = status, error
        done.cost_usd = cycle_cost(cycle_id)
        session.commit()
        elapsed = (done.finished_at - now).total_seconds()
        print(f"cycle {cycle_id} {status}, cost ${done.cost_usd:.4f}, {elapsed:.0f}s")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--no-x-fetch", action="store_true")
    parser.add_argument("--no-vision", action="store_true")
    parser.add_argument("--no-alt", action="store_true", help="skip the alternate-provider PMs")
    parser.add_argument("--trigger", help="mark the cycle as triggered by this reason")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    results = asyncio.run(
        run_cycle(
            get_config(),
            kind="triggered" if args.trigger else "scheduled",
            trigger=args.trigger,
            x_fetch=not args.no_x_fetch,
            vision=not args.no_vision,
            alt=not args.no_alt,
        )
    )
    for asset, c in results.items():
        formula = f"{c.formula_score:+.2f}" if c.formula_score is not None else "  n/a"
        line = f"{asset:8s} {c.direction.value:5s} {c.agreement.value:8s} formula {formula}"
        if c.consensus_score is not None and c.conviction is not None:
            line += f"  consensus {c.consensus_score:+.2f}  conviction {c.conviction:.2f}"
        print(line + f"  ({c.reason})")
        if c.proposal and c.proposal.entry_low:
            p = c.proposal
            print(
                f"         entry {p.entry_low:g}–{p.entry_high:g}  stop {p.stop:g}  "
                f"target {p.target:g}  hold ≤{p.max_hold_hours}h"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
