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
from app.config import Config, TradingConfig, get_config
from app.db.models import (
    AccountSnapshot,
    AgentOutputRecord,
    Candle,
    Cycle,
    DecisionRecord,
    MacroObservation,
    OrderBookSnapshot,
    PaperAccount,
    Position,
    RiskRuleHit,
    RiskState,
)
from app.decision.consensus import Agreement, Consensus
from app.decision.evidence import price_change, spot_and_atr
from app.decision.pm import Direction, Proposal
from app.risk import engine
from app.risk.exposure import betas, track_exposure
from app.risk.sleeves import (
    day_locked,
    directions_to_lock,
    lock_info,
    new_lock,
    sleeve_account,
)


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
    """The max track opens nothing since v11; its limits are the book's."""
    return lim


STOP_ROOM_MAX = Decimal(3)  # a stop is never moved more than three times further away


def widen_stop(
    c: Consensus, agg: TradingConfig | None, max_distance: Decimal | None = None
) -> Consensus:
    """Aggressive mode gives every stop more room (owner 2026-10-11): the stop moves
    ``stop_room`` times further from the middle of the entry zone. The engine sizes the
    trade on the new distance, so the position is smaller and the loss at the stop is the
    same; what changes is how easily ordinary noise stops the trade out.

    ``max_distance`` (a fraction of the entry) is where the position would reach 1x: a
    position cannot shrink below that, so widening past it would raise the loss at the
    stop. The stop is widened up to that point and never pulled closer than planned."""
    p = c.proposal
    if agg is None or p is None or not (p.stop and p.entry_low and p.entry_high):
        return c
    room = float(max(Decimal(1), min(STOP_ROOM_MAX, agg.stop_room)))
    mid = (p.entry_low + p.entry_high) / 2
    planned = abs(p.stop - mid)
    wide = planned * room
    if max_distance is not None:
        wide = max(planned, min(wide, float(max_distance) * mid))
    if wide <= planned:
        return c
    stop = mid - wide if p.stop < mid else mid + wide
    if stop <= 0:
        return c  # a long whose widened stop would fall to zero: leave it as planned
    return replace(c, proposal=p.model_copy(update={"stop": stop}))


def one_x_distance(lim: engine.Limits) -> Decimal:
    """The stop distance at which the sizing formula reaches 1x leverage, with room left
    for the hard stop that sits beyond the soft one."""
    return lim.risk_per_trade / lim.capital_share * Decimal("0.8")


LEVERAGE_HARD_MAX = Decimal(10)  # Bybit spot margin; never above, whatever the config says
RISK_HARD_MAX = Decimal("0.05")  # risk per trade, aggressive mode included
NEVER = Decimal(-1)  # a loss limit that cannot be reached: equity cannot fall by more than 100%


def aggressive_profile(cfg: Config, state: RiskState) -> TradingConfig | None:
    """v14: the one mode, always on. The name stays for the call sites."""
    return cfg.profile


def aggressive_limits(lim: engine.Limits, agg: TradingConfig | None) -> engine.Limits:
    """The account-level limits under aggressive mode: more risk per trade, a wider
    day-loss stop and exposure cap. Every value stays inside hard bounds in code."""
    if agg is None:
        return lim
    return replace(
        lim,
        # the risk per trade follows the stop cap: at the cap a stop costs capital share ×
        # cap of equity (v14: no risk-per-trade parameter)
        risk_per_trade=min(RISK_HARD_MAX, lim.capital_share * agg.stop_max_margin_pct),
        net_beta_exposure_max=max(lim.net_beta_exposure_max, agg.net_beta_exposure_max),
        net_beta_total_max=max(lim.net_beta_total_max, agg.net_beta_exposure_max),
        # no locks (owner): the day-loss stop and the drawdown pause can never be reached,
        # and there is no daily limit on trades per asset; the emergency brake stays (v14)
        day_loss_stop=NEVER,
        drawdown_pause=NEVER,
        emergency_brake=agg.emergency_brake,
        max_trades_per_asset_day=10_000,
        stop_max_margin_pct=agg.stop_max_margin_pct,
    )


def aggressive_account(
    acct: engine.AccountState, agg: TradingConfig | None, live: bool
) -> engine.AccountState:
    """The account view under aggressive mode: the full leverage ceiling (not while live:
    real money keeps the go-live ramp), and no memory of pauses, losing days or half risk.
    A brake the owner or a reconcile mismatch engaged still stands."""
    if agg is None:
        return acct
    ceiling = acct.leverage_ceiling
    if not live:
        ceiling = max(ceiling, min(Decimal(agg.leverage_ceiling), LEVERAGE_HARD_MAX))
    return engine.AccountState(
        **{
            **acct.__dict__,
            "leverage_ceiling": ceiling,
            "paused_until": None,
            "pause_count_30d": 0,
            "half_risk": False,
            "losing_days_in_row": 0,
        }
    )


def sleeve_values(s_cfg: Any, agg: TradingConfig | None) -> tuple[Decimal, Decimal, Decimal]:
    """Kept for the admin page; there are no sleeves since v14."""
    return (
        min(
            RISK_HARD_MAX,
            (agg or get_config().trading).capital_share_per_asset
            * (agg or get_config().trading).stop_max_margin_pct,
        ),
        NEVER,
        (agg or get_config().trading).net_beta_exposure_max,
    )


def cap_hold(c: Consensus, agg: TradingConfig | None) -> Consensus:
    """Aggressive mode holds no trade longer than its limit (24 hours)."""
    if agg is None or c.proposal is None or not c.proposal.max_hold_hours:
        return c
    cap = max(1, agg.max_hold_hours)
    if c.proposal.max_hold_hours <= cap:
        return c
    return replace(c, proposal=c.proposal.model_copy(update={"max_hold_hours": cap}))


def limits_from_config(cfg: Config) -> engine.Limits:
    t = cfg.trading
    return engine.Limits(
        capital_max=t.capital_max_usdt,
        risk_per_trade=min(RISK_HARD_MAX, t.capital_share_per_asset * t.stop_max_margin_pct),
        capital_share=t.capital_share_per_asset,
        leverage_max=Decimal(t.leverage_max),
        gross_exposure_max=t.gross_exposure_max,
        net_beta_exposure_max=t.net_beta_exposure_max,
        depth_cap=t.depth_cap,
        day_loss_stop=NEVER,
        drawdown_pause=NEVER,
        emergency_brake=t.emergency_brake,
        chase_max_atr=cfg.risk.chase_max_atr,
        hard_stop_atr=cfg.risk.hard_stop_atr,
    )


def risk_state(session: Session) -> RiskState:
    state = session.get(RiskState, 1)
    if state is None:
        state = RiskState(id=1, leverage_ceiling=Decimal(2))
        session.add(state)
        session.commit()
    return state


def account_state(
    session: Session,
    cfg: Config,
    mode: str,
    now: datetime,
    track: str | None = None,
    beta: dict[str, Decimal] | None = None,
) -> engine.AccountState:
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
    # Trades actually opened today on this track, per asset. Approved plans that never
    # opened (no cash, a rejected order) do not count: until 2026-10-11 they did, and three
    # approvals without a single trade used up the day's limit.
    count_track = track or ("live" if mode == "live" else "primary")
    opened_today = dict(
        session.execute(
            select(Position.asset, func.count())
            .where(Position.track == count_track, Position.opened_at >= day_start)
            .group_by(Position.asset)
        ).all()
    )
    ceiling = state.leverage_ceiling  # primary track follows live rules in shadow too (owner)
    paper = session.get(PaperAccount, 4 if mode == "live" else 1)  # the live ledger once live
    day_pnl = day_high = Decimal(0)
    if paper and paper.day_start_equity > 0:
        day_pnl = paper.equity / paper.day_start_equity - 1
        day_high = paper.day_high_equity / paper.day_start_equity - 1
    # Exposure is per track: the max and inverse tracks must not crowd out the primary.
    track = track or ("live" if mode == "live" else "primary")
    exp = track_exposure(session, track, beta or {})
    gross, net_beta = exp.gross, exp.net_beta
    starting = state.starting_capital or (paper.starting_capital if paper else None)
    return engine.AccountState(
        equity=equity,
        starting_capital=starting if starting and starting > 0 else None,
        peak_equity=peak,
        day_pnl_pct=day_pnl,
        day_high_pnl_pct=day_high,
        gross_exposure=gross,
        net_beta_exposure=net_beta,
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
    move = price_change(session, symbol, spot, now, 24)
    move_atr = Decimal(str(round(move / atr, 2))) if move is not None and atr else None
    return engine.MarketState(
        depth_quote_2pct=depth,
        atr=Decimal(str(atr)) if atr else None,
        levels=levels,
        move_24h_atr=move_atr,
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
    agg = aggressive_profile(cfg, risk_state(session))  # None unless the owner switched it on
    lim = aggressive_limits(limits_with_state(limits_from_config(cfg), risk_state(session)), agg)
    beta = betas(session, cfg, now)
    acct = account_state(session, cfg, mode, now, beta=beta)
    acct = aggressive_account(acct, agg, live=mode == "live")
    # Comparison track (shadow only): same rules with the ceiling at leverage_max, its own
    # exposure.
    max_exp = track_exposure(session, "max", beta)
    acct_max = engine.AccountState(
        **{
            **acct.__dict__,
            "leverage_ceiling": lim.leverage_max,
            "gross_exposure": max_exp.gross,
            "net_beta_exposure": max_exp.net_beta,
        }
    )
    cycle = session.get(Cycle, cycle_id)
    if cycle is not None:
        cycle.aggressive = agg is not None
    if cycle is not None and acct.equity:
        cycle.beta_exposure = {
            "betas": {a: str(b) for a, b in beta.items()},
            "primary": {
                "net_beta_x_equity": str(round(acct.net_beta_exposure / acct.equity, 3)),
                "gross_x_equity": str(round(acct.gross_exposure / acct.equity, 3)),
            },
            "max": {
                "net_beta_x_equity": str(round(max_exp.net_beta / acct.equity, 3)),
                "gross_x_equity": str(round(max_exp.gross / acct.equity, 3)),
            },
            "cap_x_equity": str(lim.net_beta_exposure_max),
        }
    account_hits = engine.account_rules(acct, lim, now)
    state = risk_state(session)
    # Sleeves (owner 2026-10-10): each has its own account view, limits and day-loss lock.
    ledger_id = 4 if mode == "live" else 1
    sleeve_accts = {
        name: sleeve_account(
            session, cfg, name, "primary" if mode != "live" else "live", now, beta, ledger_id
        )
        for name in cfg.sleeves
    }
    sleeve_accts_max = {
        name: sleeve_account(session, cfg, name, "max", now, beta, 2) for name in cfg.sleeves
    }
    # The sleeve's day-loss stop (logic v8, owner 2026-10-11): at the limit every position in
    # the sleeve is closed, and new trades are locked until 00:00 UTC only in the direction
    # that lost; the bot may follow a changed view the other way. A further loss of the
    # same size after that locks the whole sleeve for the day.
    locks: dict[str, Any] = {} if agg is not None else dict(state.day_locked_sleeves or {})
    for name, sa in sleeve_accts.items():
        if agg is not None:
            break  # aggressive mode: no day-loss stop, no lock
        s_cfg = cfg.sleeves[name]
        info = lock_info(locks, name, now)
        dirs = directions_to_lock(
            info, sa.day_pnl_pct, sleeve_values(s_cfg, agg)[1], sa.day_pnl_by_direction
        )
        if dirs is not None:
            locks[name] = new_lock(now, dirs, sa.day_pnl_pct)
            what = "new trades" if len(dirs) == 2 else f"new {next(iter(dirs))}s"
            session.add(
                RiskRuleHit(
                    cycle_id=cycle_id,
                    ts=now,
                    asset=None,
                    rule=f"day_loss_stop:{name}",
                    detail=f"{sa.day_pnl_pct:.2%} today in {name}: positions closed, "
                    f"{what} locked until 00:00 UTC",
                    effect="close_sleeve",
                )
            )
    if locks != dict(state.day_locked_sleeves or {}):
        state.day_locked_sleeves = locks
    if cycle is not None:
        cycle.beta_exposure = {
            **(cycle.beta_exposure or {}),
            "sleeves": {
                name: {
                    "net_beta_x_equity": str(round(sa.net_beta_exposure / sa.equity, 3))
                    if sa.equity
                    else None,
                    "gross_x_equity": str(round(sa.gross_exposure / sa.equity, 3))
                    if sa.equity
                    else None,
                    "day_pnl_pct": str(round(sa.day_pnl_pct, 4)),
                }
                for name, sa in sleeve_accts.items()
            },
        }
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
        c = cap_hold(consensus_from_row(d), agg)
        mkt = replace(
            market_state(session, cfg, d.asset, cycle_id, now, c.direction),
            beta=beta.get(d.asset, Decimal(1)),
        )
        sleeve_name = cfg.sleeve_of(d.asset)
        if sleeve_name is not None:
            s_cfg = cfg.sleeves[sleeve_name]
            sa = sleeve_accts[sleeve_name]
            risk_s, stop_s, beta_s = sleeve_values(s_cfg, agg)
            lim_s = replace(
                lim,
                risk_per_trade=risk_s,
                leverage_max=Decimal(s_cfg.leverage_max),
                net_beta_exposure_max=beta_s,
                day_loss_stop=stop_s,
            )
            c = widen_stop(c, agg, one_x_distance(lim_s))
            acct_s = engine.AccountState(
                **{
                    **acct.__dict__,
                    "equity": sa.equity,
                    "day_pnl_pct": sa.day_pnl_pct,
                    "gross_exposure": sa.gross_exposure,
                    "net_beta_exposure": sa.net_beta_exposure,
                    "sleeve": sleeve_name,
                    "total_equity": acct.equity,
                    "total_net_beta_exposure": sum(
                        (x.net_beta_exposure for x in sleeve_accts.values()), Decimal(0)
                    ),
                    "leverage_ceiling": min(acct.leverage_ceiling, Decimal(s_cfg.leverage_max)),
                }
            )
            hits_s = list(account_hits)
            if agg is None and day_locked(locks, sleeve_name, now, c.direction.value):
                hits_s.append(
                    engine.RuleHit(
                        f"day_loss_stop:{sleeve_name}",
                        f"new {c.direction.value}s locked until 00:00 UTC",
                        "block",
                    )
                )
            sm = sleeve_accts_max[sleeve_name]
            acct_s_max = engine.AccountState(
                **{
                    **acct_s.__dict__,
                    "leverage_ceiling": Decimal(s_cfg.leverage_max),
                    "equity": sm.equity,
                    "gross_exposure": sm.gross_exposure,
                    "net_beta_exposure": sm.net_beta_exposure,
                    "total_net_beta_exposure": sum(
                        (x.net_beta_exposure for x in sleeve_accts_max.values()), Decimal(0)
                    ),
                }
            )
            a = engine.assess(c, acct_s, mkt, lim_s, account_hits=hits_s)
            a_max = (
                engine.assess(c, acct_s_max, mkt, limits_for_max(lim_s, cfg), account_hits=hits_s)
                if mode != "live"
                else None
            )
        else:
            c = widen_stop(c, agg, one_x_distance(lim))
            a = engine.assess(c, acct, mkt, lim, account_hits=account_hits)
            a_max = (
                engine.assess(c, acct_max, mkt, limits_for_max(lim, cfg), account_hits=account_hits)
                if mode != "live"
                else None
            )
        out[d.asset] = a
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
