"""Glue between the stored decisions and the risk engine; runs at the end of a cycle.

Reads the account state (paper equity in shadow mode until the simulator exists; the
exchange snapshot in live mode), the market state per asset, assesses every decision,
and writes the rule hits and the resulting action back. The executor (M6) reads the
``decisions`` rows with ``action = "open"`` and the stored plan.
"""

from __future__ import annotations

from dataclasses import replace
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
    Candle,
    DecisionRecord,
    MacroObservation,
    OrderBookSnapshot,
    PaperAccount,
    Position,
    RiskRuleHit,
    RiskState,
)
from app.decision.consensus import Agreement, Consensus
from app.decision.evidence import spot_and_atr
from app.decision.pm import Direction, Proposal
from app.risk import engine


def levels_for(session: Session, symbol: str, spot: Decimal) -> tuple[Decimal, ...]:
    """Round numbers within ±8% of spot plus the swing lows/highs of the last 48 hours."""
    from app.risk.stops import round_levels, swing_levels

    rows = session.execute(
        select(Candle.high, Candle.low)
        .where(Candle.symbol == symbol, Candle.interval == "60")
        .order_by(Candle.open_time.desc())
        .limit(48)
    ).all()
    highs = [Decimal(str(r.high)) for r in reversed(rows)]
    lows = [Decimal(str(r.low)) for r in reversed(rows)]
    band = spot * Decimal("0.08")
    found = round_levels(spot, spot - band, spot + band) + swing_levels(highs, lows)
    return tuple(sorted({lv for lv in found if spot - band <= lv <= spot + band}))


def limits_with_state(lim: engine.Limits, state: RiskState) -> engine.Limits:
    """The tuned stop multiples live in risk_state (wick-out tuning)."""
    return replace(
        lim,
        stop_buffer_atr=state.stop_buffer_atr or lim.stop_buffer_atr,
        hard_stop_atr=state.hard_stop_atr or lim.hard_stop_atr,
    )


def limits_for_max(lim: engine.Limits, cfg: Config) -> engine.Limits:
    """The max track's limits: the same rules, sized with ``risk_per_trade_max``."""
    return replace(lim, risk_per_trade=cfg.trading.risk_per_trade_max)


def limits_from_config(cfg: Config) -> engine.Limits:
    t = cfg.trading
    return engine.Limits(
        capital_max=t.capital_max_usdt,
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
        exchange_equity = Decimal(str(snap.total_equity or 0)) if snap else Decimal(0)
        # Live: the capital ramp's share of capital_max (10% at the start), never beyond
        # what the subaccount actually holds.
        fraction = state.capital_fraction or cfg.trading.live_start_fraction
        equity = min(exchange_equity, cfg.trading.capital_max_usdt * fraction)
    else:
        paper = session.get(PaperAccount, 1)
        equity = paper.equity if paper and paper.equity > 0 else cfg.trading.capital_max_usdt
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
    ceiling = state.leverage_ceiling  # primary track follows live rules in shadow too (owner)
    paper = session.get(PaperAccount, 4 if mode == "live" else 1)  # the live ledger once live
    day_pnl = day_high = Decimal(0)
    if paper and paper.day_start_equity > 0:
        day_pnl = paper.equity / paper.day_start_equity - 1
        day_high = paper.day_high_equity / paper.day_start_equity - 1
    gross = Decimal(
        str(
            session.execute(
                select(func.coalesce(func.sum(Position.notional), 0)).where(
                    Position.status == "open"
                )
            ).scalar_one()
        )
    )
    starting = state.starting_capital or (paper.starting_capital if paper else None)
    return engine.AccountState(
        equity=equity,
        starting_capital=starting if starting and starting > 0 else None,
        peak_equity=peak,
        day_pnl_pct=day_pnl,
        day_high_pnl_pct=day_high,
        gross_exposure=gross,
        trades_today={k: int(v) for k, v in opened_today.items()},
        losing_days_in_row=losing_days_in_row(session, now),
        paused_until=state.paused_until,
        pause_count_30d=state.pause_count_30d,
        emergency_brake=state.emergency_brake,
        half_risk=bool(state.half_risk_until and state.half_risk_until > now),
        leverage_ceiling=ceiling,
    )


def losing_days_in_row(session: Session, now: datetime) -> int:
    """Consecutive calendar days (before today) with negative realized P&L."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    streak = 0
    for i in range(1, 8):
        start, end = day - timedelta(days=i), day - timedelta(days=i - 1)
        pnl = session.execute(
            select(func.coalesce(func.sum(Position.pnl), 0)).where(
                Position.status == "closed", Position.closed_at >= start, Position.closed_at < end
            )
        ).scalar_one()
        if Decimal(str(pnl)) < 0:
            streak += 1
        else:
            break
    return streak


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
    depth, truncated = Decimal(0), True
    if book is not None:
        depth = Decimal(
            str(book.depth_ask_2pct if direction is Direction.LONG else book.depth_bid_2pct)
        )
        truncated = bool(book.depth_truncated)
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
    borrow_rate, collateral_ratio = margin_terms(session, cfg.base_coin(asset))
    spot, atr = spot_and_atr(session, symbol)
    levels = levels_for(session, symbol, Decimal(str(spot))) if spot else ()
    return engine.MarketState(
        depth_quote_2pct=depth,
        atr=Decimal(str(atr)) if atr else None,
        levels=levels,
        depth_truncated=truncated,
        atr_extreme=atr_extreme,
        event_today=event_today(now),
        risk_off_coupled=risk_off and coupling > 0.5,
        fng_extreme=fng_extreme,
        taker_fee=fees,
        hourly_borrow_rate=borrow_rate,
        margin_enabled=True,  # all USDT pairs have margin (M0 report); the executor re-checks
        short_allowed=cfg.trading.short_allowed,
        collateral_ratio=collateral_ratio,
        maintenance_rate=cfg.risk.maintenance_margin_rate,
    )


def _plan_dict(p: engine.TradePlan, atr: float | None = None) -> dict[str, Any]:
    """The executor's instructions. ``atr`` (14 × 4h) sizes the exchange-side backup
    stop; it is absent when there are too few candles."""
    plan: dict[str, Any] = {
        "entry": str(p.entry),
        "stop": str(p.stop),
        "hard_stop": str(p.hard_stop) if p.hard_stop is not None else str(p.stop),
        "target": str(p.target),
        "max_hold_hours": p.max_hold_hours,
        "leverage": str(p.leverage),
        "borrow": p.borrow,
        "notional": str(p.notional),
        "margin": str(p.margin),
    }
    if atr:
        plan["atr"] = f"{atr:.10g}"
    return plan


def margin_terms(session: Session, coin: str) -> tuple[Decimal, Decimal]:
    """Latest hourly borrow rate and collateral ratio stored by the executor's refresh."""
    row = session.execute(
        select(MacroObservation.value, MacroObservation.date)
        .where(MacroObservation.series == f"BORROW_{coin}")
        .order_by(MacroObservation.date.desc())
        .limit(1)
    ).first()
    cr = session.execute(
        select(MacroObservation.value)
        .where(MacroObservation.series == f"COLLATERAL_{coin}")
        .order_by(MacroObservation.date.desc())
        .limit(1)
    ).scalar_one_or_none()
    rate = Decimal(str(row.value)) if row else Decimal("0.000005")
    return rate, Decimal(str(cr)) if cr is not None else Decimal("0.98")


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
    lim = limits_with_state(limits_from_config(cfg), risk_state(session))
    acct = account_state(session, cfg, mode, now)
    # Comparison track (shadow only): same rules with the ceiling at leverage_max.
    acct_max = engine.AccountState(**{**acct.__dict__, "leverage_ceiling": lim.leverage_max})
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
        a_max = (
            engine.assess(c, acct_max, mkt, limits_for_max(lim, cfg), account_hits=account_hits)
            if mode != "live"
            else None
        )
        hits: list[dict[str, Any]] = [
            {"rule": h.rule, "detail": h.detail, "effect": h.effect} for h in a.hits
        ]
        d.risk_rule_hits = hits
        d.action = "open" if a.allowed else "none"
        _, atr = spot_and_atr(session, cfg.symbol(d.asset))
        if a_max is not None and a_max.plan is not None:
            d.proposal = {**(d.proposal or {}), "plan_max": _plan_dict(a_max.plan, atr)}
        if a.plan is not None:
            d.proposal = {**(d.proposal or {}), "plan": _plan_dict(a.plan, atr)}
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
