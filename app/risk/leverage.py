"""Leverage agent (SPEC §8): base formula, then the lowest of every cap. Pure functions.

    L = min(leverage_max, r / (a × d))
    r = risk per trade, a = capital share per asset, d = stop distance (fraction)

Then the minimum over the caps table. An LLM may add a cap (``llm_cap``) but can only
lower the result, never raise it. Everything here is deterministic and unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.decision.consensus import Agreement

ONE = Decimal(1)


@dataclass(frozen=True)
class LeverageInputs:
    asset: str
    stop_distance: Decimal  # fraction of entry, e.g. 0.01 for 1%
    risk_per_trade: Decimal  # 0.005
    capital_share: Decimal  # 0.20
    leverage_max: Decimal  # 10
    leverage_max_spx6900: Decimal  # 3
    ceiling: Decimal  # live ramp: 2 → 5 → 10 (§8); shadow uses leverage_max
    atr_extreme: bool  # ATR above its 30-day 90th percentile
    event_today: bool  # FOMC / CPI / jobs report today
    risk_off_coupled: bool  # risk-off regime while coupling > 0.5
    agreement: Agreement
    conviction: Decimal
    two_losing_days: bool
    half_risk: bool  # after a drawdown pause (§8)
    fng_extreme: bool = False  # owner briefing: Fear & Greed extreme is a caution flag
    llm_cap: Decimal | None = None  # an LLM may only lower


@dataclass(frozen=True)
class LeverageResult:
    base: Decimal
    leverage: Decimal
    borrow_allowed: bool
    caps: list[tuple[str, Decimal]] = field(default_factory=list)  # every cap that applied


def base_leverage(inp: LeverageInputs) -> Decimal:
    if inp.stop_distance <= 0:
        return ONE
    raw = inp.risk_per_trade / (inp.capital_share * inp.stop_distance)
    return min(inp.leverage_max, raw)


def compute(inp: LeverageInputs) -> LeverageResult:
    base = base_leverage(inp)
    if inp.half_risk:
        base = base / 2
    caps: list[tuple[str, Decimal]] = [("absolute", inp.leverage_max), ("ceiling", inp.ceiling)]
    if inp.asset == "SPX6900":
        caps.append(("spx6900", inp.leverage_max_spx6900))
    if inp.atr_extreme:
        caps.append(("atr_extreme", base / 2))
    if inp.event_today:
        caps.append(("event_today", Decimal(2)))
    if inp.risk_off_coupled:
        caps.append(("risk_off_coupled", Decimal(2)))
    if inp.fng_extreme:
        caps.append(("fng_extreme", Decimal(2)))
    borrow_allowed = True
    if inp.agreement is not Agreement.AGREE or inp.conviction < Decimal("0.5"):
        caps.append(("disagree_or_low_conviction", ONE))
        borrow_allowed = False
    if inp.two_losing_days:
        caps.append(("two_losing_days", base / 2))
    if inp.llm_cap is not None:
        caps.append(("llm", max(ONE, inp.llm_cap)))
    leverage = max(ONE, min([base, *(c for _, c in caps)]))
    applied = [(name, c) for name, c in caps if c <= leverage]
    return LeverageResult(base=base, leverage=leverage, borrow_allowed=borrow_allowed, caps=applied)


def liquidation_distance(
    leverage: Decimal,
    *,
    short: bool = False,
    collateral_ratio: Decimal = Decimal("0.98"),
    maintenance_rate: Decimal = Decimal("0.03"),
) -> Decimal:
    """Distance to liquidation as a fraction of entry, Bybit cross-margin rule per position.

    Long with leverage L: margin 1/L of notional, borrowed (L−1)/L; liquidation when
    c·P/P0 ≤ (L−1)/L·(1+mm). Short: liquidation when 1/L + 1 ≤ P/P0·(1+mm).
    """
    if leverage <= 1 and not short:
        return ONE  # nothing borrowed: cannot be liquidated
    if short:
        return (ONE + ONE / leverage) / (ONE + maintenance_rate) - ONE
    borrowed = (leverage - ONE) / leverage
    return ONE - borrowed * (ONE + maintenance_rate) / collateral_ratio


def liquidation_buffer_ok(
    leverage: Decimal,
    stop_distance: Decimal,
    *,
    short: bool = False,
    collateral_ratio: Decimal = Decimal("0.98"),
    maintenance_rate: Decimal = Decimal("0.03"),
) -> bool:
    """Liquidation must sit at least 3× the stop distance away (§8)."""
    dist = liquidation_distance(
        leverage, short=short, collateral_ratio=collateral_ratio, maintenance_rate=maintenance_rate
    )
    return dist >= 3 * stop_distance


def reduce_for_liquidation(
    leverage: Decimal,
    stop_distance: Decimal,
    *,
    short: bool = False,
    collateral_ratio: Decimal = Decimal("0.98"),
    maintenance_rate: Decimal = Decimal("0.03"),
) -> Decimal:
    """Lower leverage in 0.5 steps until the liquidation buffer holds; never below 1."""
    lev = leverage
    while lev > ONE and not liquidation_buffer_ok(
        lev,
        stop_distance,
        short=short,
        collateral_ratio=collateral_ratio,
        maintenance_rate=maintenance_rate,
    ):
        lev = max(ONE, lev - Decimal("0.5"))
    return lev
