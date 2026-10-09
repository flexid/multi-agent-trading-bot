"""Glue between the stored decisions and the risk engine; runs at the end of a cycle.

Reads the account state (paper equity in shadow mode until the simulator exists; the
exchange snapshot in live mode), the market state per asset, assesses every decision,
and writes the rule hits and the resulting action back. The executor (M6) reads the
``decisions`` rows with ``action = "open"`` and the stored plan.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.macro import event_today
from app.config import Config
from app.db.models import (
    AccountSnapshot,
    AgentOutputRecord,
    DecisionRecord,
    OrderBookSnapshot,
    RiskRuleHit,
    RiskState,
)
from app.decision.consensus import Agreement, Consensus
from app.decision.pm import Direction, Proposal
from app.risk import engine


def limits_from_config(cfg: Config) -> engine.Limits:
    t = cfg.trading
    return engine.Limits(
        capital_max=t.capital_max_usdc,
        risk_per_trade=t.risk_per_trade,
        capital_share=t.capital_share_per_asset,
        leverage_max=Decimal(t.leverage_max),
        leverage_max_spx6900=Decimal(t.leverage_max_spx6900),
        gross_exposure_max=t.gross_exposure_max,
        depth_cap=t.depth_cap,
        day_loss_stop=t.day_loss_stop,
        drawdown_pause=t.drawdown_pause,
        emergency_brake=t.emergency_brake,
    )


def risk_state(session: Session) -> RiskState:
    state = session.get(RiskState, 1)
    if state is None:
        state = RiskState(id=1, leverage_ceiling=Decimal(2))
        session.add(state)
        session.commit()
    return state


def account_state(session: Session, cfg: Config, mode: str, now: datetime) -> engine.AccountState:
    state = risk_state(session)
    if mode == "live":
        snap = session.execute(
            select(AccountSnapshot).order_by(AccountSnapshot.ts.desc()).limit(1)
        ).scalar_one_or_none()
        equity = Decimal(str(snap.total_equity or 0)) if snap else Decimal(0)
    else:
        equity = cfg.trading.capital_max_usdc  # paper equity until the simulator (M6) tracks it
    peak = max(state.peak_equity or Decimal(0), equity)
    if state.peak_equity != peak:
        state.peak_equity, state.updated_at = peak, now
        session.commit()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    opened_today = dict(
        session.execute(
            select(DecisionRecord.asset, func.count())
            .where(DecisionRecord.ts >= day_start, DecisionRecord.action == "open")
            .group_by(DecisionRecord.asset)
        ).all()
    )
    ceiling = state.leverage_ceiling if mode == "live" else Decimal(cfg.trading.leverage_max)
    return engine.AccountState(
        equity=equity,
        starting_capital=state.starting_capital,
        peak_equity=peak,
        day_pnl_pct=Decimal(0),  # from the simulator / fills (M6)
        day_high_pnl_pct=Decimal(0),
        gross_exposure=Decimal(0),  # from positions (M6)
        trades_today={k: int(v) for k, v in opened_today.items()},
        losing_days_in_row=0,
        paused_until=state.paused_until,
        pause_count_30d=state.pause_count_30d,
        emergency_brake=state.emergency_brake,
        half_risk=bool(state.half_risk_until and state.half_risk_until > now),
        leverage_ceiling=ceiling,
    )


def market_state(
    session: Session, cfg: Config, asset: str, cycle_id: int, now: datetime, direction: Direction
) -> engine.MarketState:
    symbol = cfg.symbol(asset)
    book = session.execute(
        select(OrderBookSnapshot)
        .where(OrderBookSnapshot.symbol == symbol)
        .order_by(OrderBookSnapshot.ts.desc())
        .limit(1)
    ).scalar_one_or_none()
    depth = Decimal(0)
    if book is not None:
        depth = Decimal(
            str(book.depth_ask_2pct if direction is Direction.LONG else book.depth_bid_2pct)
        )
    flags = session.execute(
        select(
            AgentOutputRecord.agent, AgentOutputRecord.risk_flags, AgentOutputRecord.evidence
        ).where(AgentOutputRecord.cycle_id == cycle_id, AgentOutputRecord.asset == asset)
    ).all()
    all_flags = [f for _, fl, _ in flags for f in fl]
    atr_extreme = any("ATR above" in f for f in all_flags)
    fng_extreme = any("fear & greed extreme" in f for f in all_flags)
    macro_line = next((ev[0] for a, _, ev in flags if a == "macro" and ev), "")
    risk_off = "risk_off" in macro_line
    try:
        coupling = float(macro_line.split("coupling ")[1])
    except (IndexError, ValueError):
        coupling = 0.0
    fees = cfg_fee(cfg)
    return engine.MarketState(
        depth_quote_2pct=depth,
        atr_extreme=atr_extreme,
        event_today=event_today(now),
        risk_off_coupled=risk_off and coupling > 0.5,
        fng_extreme=fng_extreme,
        taker_fee=fees,
        hourly_borrow_rate=Decimal("0.000005"),  # refreshed from the exchange in M6
        margin_enabled=True,  # USDT pairs all have margin (M0 report); verified in M6
        short_allowed=cfg.trading.short_allowed,
    )


def cfg_fee(cfg: Config) -> Decimal:
    return Decimal("0.001") if cfg.exchange.quote == "USDT" else Decimal("0.0005")


def consensus_from_row(d: DecisionRecord) -> Consensus:
    proposal = Proposal.model_validate(d.proposal) if d.proposal else None
    return Consensus(
        direction=Direction(d.direction),
        agreement=Agreement(d.agreement),
        formula_score=d.formula_score,
        consensus_score=d.consensus_score,
        conviction=d.conviction,
        valid_agents=d.valid_agents,
        reason=d.reason,
        proposal=proposal,
    )


def apply_risk(
    session: Session, cfg: Config, cycle_id: int, mode: str, now: datetime | None = None
) -> dict[str, engine.Assessment]:
    now = now or datetime.now(UTC)
    lim = limits_from_config(cfg)
    acct = account_state(session, cfg, mode, now)
    account_hits = engine.account_rules(acct, lim, now)
    state = risk_state(session)
    for h in account_hits:
        session.add(
            RiskRuleHit(
                cycle_id=cycle_id, ts=now, asset=None, rule=h.rule, detail=h.detail, effect=h.effect
            )
        )
        if h.effect == "pause" and not state.paused_until:
            state.paused_until = engine.pause_until(now)
            state.half_risk_until = now + timedelta(days=30)
            state.pause_count_30d += 1
            state.last_pause_at = now
            state.leverage_ceiling = min(state.leverage_ceiling, Decimal(2))
        if h.effect == "brake":
            state.emergency_brake, state.brake_reason = True, h.detail
        if h.effect == "close_all" and not state.day_locked_until:
            state.day_locked_until = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)
    state.updated_at = now
    out: dict[str, engine.Assessment] = {}
    rows = session.scalars(select(DecisionRecord).where(DecisionRecord.cycle_id == cycle_id)).all()
    for d in rows:
        c = consensus_from_row(d)
        mkt = market_state(session, cfg, d.asset, cycle_id, now, c.direction)
        a = engine.assess(c, acct, mkt, lim, account_hits=account_hits)
        out[d.asset] = a
        hits: list[dict[str, Any]] = [
            {"rule": h.rule, "detail": h.detail, "effect": h.effect} for h in a.hits
        ]
        d.risk_rule_hits = hits
        d.action = "open" if a.allowed else "none"
        if a.plan is not None:
            d.proposal = {
                **(d.proposal or {}),
                "plan": {
                    "entry": str(a.plan.entry),
                    "stop": str(a.plan.stop),
                    "target": str(a.plan.target),
                    "max_hold_hours": a.plan.max_hold_hours,
                    "leverage": str(a.plan.leverage),
                    "borrow": a.plan.borrow,
                    "notional": str(a.plan.notional),
                    "margin": str(a.plan.margin),
                },
            }
        for h in a.hits:
            if h.effect in ("cap", "block") and h.rule not in {x.rule for x in account_hits}:
                session.add(
                    RiskRuleHit(
                        cycle_id=cycle_id,
                        ts=now,
                        asset=d.asset,
                        rule=h.rule,
                        detail=h.detail,
                        effect=h.effect,
                    )
                )
    session.commit()
    return out
