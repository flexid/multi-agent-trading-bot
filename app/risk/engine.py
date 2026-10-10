"""Risk engine (SPEC §8): the deterministic gate between a decision and an order.

``assess`` takes one consensus plus the account/day state and returns either a sized
``TradePlan`` or a block with the rule that fired. ``account_rules`` evaluates the
account-level limits (day loss, lock, drawdown, emergency brake) that apply before any
asset is looked at. Pure functions; the cycle runner persists their results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from app.decision.consensus import Agreement, Consensus
from app.decision.pm import Direction
from app.risk import leverage as lev
from app.risk.stops import place_stop

ZERO = Decimal(0)


@dataclass(frozen=True)
class AccountState:
    equity: Decimal
    starting_capital: Decimal | None
    peak_equity: Decimal
    day_pnl_pct: Decimal  # realized + unrealized since 00:00 UTC, fraction
    day_high_pnl_pct: Decimal  # best day P&L seen today, fraction
    gross_exposure: Decimal  # sum of |position notional|, in quote
    trades_today: dict[str, int]  # asset -> opened today
    losing_days_in_row: int
    paused_until: datetime | None
    pause_count_30d: int
    emergency_brake: bool
    half_risk: bool
    leverage_ceiling: Decimal


@dataclass(frozen=True)
class MarketState:
    depth_quote_2pct: Decimal  # resting quote within ±2% on the entry side
    atr_extreme: bool
    event_today: bool
    risk_off_coupled: bool
    fng_extreme: bool
    taker_fee: Decimal
    hourly_borrow_rate: Decimal
    margin_enabled: bool
    short_allowed: bool  # base coin borrowable
    collateral_ratio: Decimal = Decimal("0.98")  # of the base coin, from the API
    maintenance_rate: Decimal = Decimal("0.03")
    depth_truncated: bool = False  # 200 levels did not reach ±2%: depth is a lower bound
    atr: Decimal | None = None  # ATR(14, 4h); stop placement needs it
    levels: tuple[Decimal, ...] = ()  # round numbers and recent swings near the price


@dataclass(frozen=True)
class Limits:
    capital_max: Decimal
    risk_per_trade: Decimal
    capital_share: Decimal
    leverage_max: Decimal
    leverage_max_spx6900: Decimal
    gross_exposure_max: Decimal  # × equity
    depth_cap: Decimal  # 0.05
    day_loss_stop: Decimal  # −0.02
    day_lock_profit: Decimal = Decimal("0.015")  # from +1.5% …
    day_lock_floor: Decimal = Decimal("0.0075")  # … protect +0.75%
    drawdown_pause: Decimal = Decimal("-0.10")
    emergency_brake: Decimal = Decimal("-0.25")
    max_trades_per_asset_day: int = 3
    fee_multiple: Decimal = Decimal(3)
    stop_buffer_atr: Decimal = Decimal("0.5")  # beyond a level, in ATR (tuned by wick-outs)
    hard_stop_atr: Decimal = Decimal(1)  # the touch stop sits this far beyond the soft one
    min_valid_agents: int = 3


@dataclass(frozen=True)
class RuleHit:
    rule: str
    detail: str
    effect: str  # cap | block | close_all | pause | brake | protect


@dataclass(frozen=True)
class TradePlan:
    asset: str
    direction: Direction
    entry: Decimal
    stop: Decimal
    target: Decimal
    max_hold_hours: int
    leverage: Decimal
    borrow: bool
    notional: Decimal  # quote value of the position
    margin: Decimal  # own capital committed
    hits: list[RuleHit] = field(default_factory=list)
    hard_stop: Decimal | None = None  # touch stop; ``stop`` is the soft, close-based one


@dataclass(frozen=True)
class Assessment:
    asset: str
    allowed: bool
    plan: TradePlan | None
    hits: list[RuleHit]


# --- account level ------------------------------------------------------------------


def account_rules(acct: AccountState, lim: Limits, now: datetime | None = None) -> list[RuleHit]:
    """Limits that stop everything, in order of severity."""
    now = now or datetime.now(UTC)
    hits: list[RuleHit] = []
    if acct.emergency_brake:
        hits.append(RuleHit("emergency_brake", "brake engaged; waiting for the owner", "brake"))
        return hits
    if acct.starting_capital and acct.starting_capital > 0:
        total = acct.equity / acct.starting_capital - 1
        if total <= lim.emergency_brake:
            hits.append(
                RuleHit("emergency_brake", f"{total:.1%} against starting capital", "brake")
            )
            return hits
    if acct.pause_count_30d >= 2:
        hits.append(RuleHit("emergency_brake", "two pauses within 30 days", "brake"))
        return hits
    if acct.peak_equity > 0:
        dd = acct.equity / acct.peak_equity - 1
        if dd <= lim.drawdown_pause and not acct.paused_until:
            hits.append(
                RuleHit("drawdown_pause", f"{dd:.1%} from peak: close all, 72 h pause", "pause")
            )
            return hits
    if acct.paused_until and acct.paused_until > now:
        hits.append(RuleHit("paused", f"until {acct.paused_until:%Y-%m-%d %H:%M} UTC", "block"))
        return hits
    if acct.day_pnl_pct <= lim.day_loss_stop:
        hits.append(
            RuleHit(
                "day_loss_stop",
                f"{acct.day_pnl_pct:.2%} today: close all until 00:00 UTC",
                "close_all",
            )
        )
        return hits
    if acct.day_high_pnl_pct >= lim.day_lock_profit:
        hits.append(
            RuleHit(
                "day_profit_lock",
                f"day high {acct.day_high_pnl_pct:.2%}: "
                f"stops moved to lock {lim.day_lock_floor:.2%}",
                "protect",
            )
        )
    return hits


# --- per trade ------------------------------------------------------------------------


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def assess(
    c: Consensus,
    acct: AccountState,
    mkt: MarketState,
    lim: Limits,
    *,
    account_hits: list[RuleHit] | None = None,
) -> Assessment:
    hits: list[RuleHit] = list(account_hits or [])
    asset = c.proposal.asset if c.proposal else "?"
    blocking = {"brake", "pause", "block", "close_all"}
    if any(h.effect in blocking for h in hits):
        return Assessment(asset, False, None, hits)
    if c.direction is Direction.FLAT or c.proposal is None or c.conviction is None:
        hits.append(RuleHit("no_consensus", c.reason, "block"))
        return Assessment(asset, False, None, hits)
    if c.valid_agents < lim.min_valid_agents:
        hits.append(RuleHit("valid_agents", f"{c.valid_agents} < {lim.min_valid_agents}", "block"))
        return Assessment(asset, False, None, hits)
    if acct.trades_today.get(asset, 0) >= lim.max_trades_per_asset_day:
        hits.append(RuleHit("trades_per_day", f"{acct.trades_today[asset]} already today", "block"))
        return Assessment(asset, False, None, hits)
    p = c.proposal
    assert p.entry_low and p.entry_high and p.stop and p.target and p.max_hold_hours
    entry = Decimal(str((p.entry_low + p.entry_high) / 2))
    stop, target = Decimal(str(p.stop)), Decimal(str(p.target))
    # Stop hunts (owner 2026-10-10): the soft stop moves clear of levels, the hard stop
    # sits an ATR beyond it, and the size is set by the hard stop.
    placed = place_stop(
        c.direction,
        entry,
        stop,
        mkt.atr,
        mkt.levels,
        buffer=lim.stop_buffer_atr,
        hard=lim.hard_stop_atr,
    )
    if placed.moved:
        hits.append(RuleHit("stop_buffer", placed.reason, "adjust"))
    stop, hard_stop = placed.soft, placed.hard
    stop_distance = abs(entry - hard_stop) / entry
    target_distance = abs(target - entry) / entry

    if c.direction is Direction.SHORT and not (mkt.margin_enabled and mkt.short_allowed):
        hits.append(RuleHit("short_unavailable", "no margin borrow for this pair", "block"))
        return Assessment(asset, False, None, hits)

    inputs = lev.LeverageInputs(
        asset=asset,
        stop_distance=stop_distance,
        risk_per_trade=lim.risk_per_trade,
        capital_share=lim.capital_share,
        leverage_max=lim.leverage_max,
        leverage_max_spx6900=lim.leverage_max_spx6900,
        ceiling=acct.leverage_ceiling,
        atr_extreme=mkt.atr_extreme,
        event_today=mkt.event_today,
        risk_off_coupled=mkt.risk_off_coupled,
        agreement=c.agreement,
        conviction=Decimal(str(c.conviction)),
        two_losing_days=acct.losing_days_in_row >= 2,
        half_risk=acct.half_risk,
        fng_extreme=mkt.fng_extreme,
    )
    result = lev.compute(inputs)
    leverage = result.leverage
    for name, cap in result.caps:
        hits.append(RuleHit(f"leverage_cap:{name}", f"{cap:.2f}x", "cap"))
    if not mkt.margin_enabled:
        leverage = Decimal(1)
        hits.append(RuleHit("leverage_cap:no_margin", "pair has no margin: 1x", "cap"))
    if leverage > 1:
        safe = lev.reduce_for_liquidation(
            leverage,
            stop_distance,
            short=c.direction is Direction.SHORT,
            collateral_ratio=mkt.collateral_ratio,
            maintenance_rate=mkt.maintenance_rate,
        )
        if safe < leverage:
            hits.append(RuleHit("liquidation_buffer", f"{leverage:.2f}x → {safe:.2f}x", "cap"))
            leverage = safe
    # A short always borrows the coin; a long borrows only above 1x.
    borrow = result.borrow_allowed and (leverage > 1 or c.direction is Direction.SHORT)
    if c.direction is Direction.SHORT and not borrow:
        hits.append(
            RuleHit("short_needs_borrow", "PMs disagree or conviction < 0.5: no borrowing", "block")
        )
        return Assessment(asset, False, None, hits)

    # Cost rule: target must cover 3× fees plus borrow interest for the planned hold.
    fees = 2 * mkt.taker_fee
    interest = (
        mkt.hourly_borrow_rate * p.max_hold_hours * (leverage - 1) / leverage if borrow else ZERO
    )
    required = lim.fee_multiple * fees + interest
    if target_distance < required:
        hits.append(
            RuleHit("cost_rule", f"target {target_distance:.2%} < required {required:.2%}", "block")
        )
        return Assessment(asset, False, None, hits)

    # Size: capital share × leverage, capped by depth and gross exposure.
    capital = min(lim.capital_max, acct.equity) if lim.capital_max > 0 else acct.equity
    margin = capital * lim.capital_share
    if acct.half_risk:
        margin /= 2
    notional = margin * leverage
    depth_cap = mkt.depth_quote_2pct * lim.depth_cap
    if mkt.depth_truncated:
        hits.append(
            RuleHit("depth_truncated", "book ends inside ±2%: depth is a lower bound", "flag")
        )
    if notional > depth_cap:
        hits.append(
            RuleHit("depth_cap", f"{notional:.0f} > 5% of ±2% depth ({depth_cap:.0f})", "cap")
        )
        notional = depth_cap
    room = lim.gross_exposure_max * acct.equity - acct.gross_exposure
    if notional > room:
        hits.append(RuleHit("gross_exposure", f"{notional:.0f} > room {room:.0f}", "cap"))
        notional = max(ZERO, room)
    if notional <= 0:
        hits.append(RuleHit("no_room", "no exposure room left", "block"))
        return Assessment(asset, False, None, hits)
    margin = notional / leverage
    plan = TradePlan(
        asset=asset,
        direction=c.direction,
        entry=entry,
        stop=stop,
        hard_stop=hard_stop,
        target=target,
        max_hold_hours=p.max_hold_hours,
        leverage=_round_down(leverage, Decimal("0.1")),
        borrow=borrow,
        notional=_round_down(notional, Decimal("0.01")),
        margin=_round_down(margin, Decimal("0.01")),
        hits=hits,
    )
    return Assessment(asset, True, plan, hits)


def pause_until(now: datetime) -> datetime:
    return now + timedelta(hours=72)


__all__ = [
    "AccountState",
    "Agreement",
    "Assessment",
    "Limits",
    "MarketState",
    "RuleHit",
    "TradePlan",
    "account_rules",
    "assess",
    "pause_until",
]
