"""The fixed rules (bot spec §1), enforced in code and never by a model. Pure functions.

Fee maths: a round trip costs two taker legs of the notional, so ``2 × fee × leverage``
percent of the margin. A net target T on margin needs a gross move of ``T + fees`` on
margin, i.e. a price move of ``(T + fees) / leverage``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

LOSS_NET_ROE = Decimal(0)  # a trade at or below this is a loss


@dataclass(frozen=True)
class Rules:
    max_leverage: int = 10
    tp_min: int = 2  # net ROE %, inclusive
    tp_max: int = 10
    losses_to_bench: int = 2
    taker_fee_pct: Decimal = Decimal("0.055")  # per side, percent of notional
    min_conviction_floor: float = 0.5
    fee_share_max: Decimal = Decimal("0.5")  # fees may eat at most half the target


def round_trip_fee_pct(rules: Rules, leverage: int) -> Decimal:
    """Both fee legs as a percentage of the margin."""
    return 2 * rules.taker_fee_pct * leverage


def gross_roe_pct(rules: Rules, net_roe_pct: Decimal, leverage: int) -> Decimal:
    """The gross return on margin that nets ``net_roe_pct`` after both fee legs."""
    return net_roe_pct + round_trip_fee_pct(rules, leverage)


def price_move_pct(rules: Rules, net_roe_pct: Decimal, leverage: int) -> Decimal:
    """The price move (percent) that nets ``net_roe_pct`` on margin at ``leverage``."""
    return gross_roe_pct(rules, net_roe_pct, leverage) / leverage


@dataclass(frozen=True)
class Plan:
    """A rulebook's leverage, take and stop after the rules: what the engine trades with."""

    leverage: int
    tp_net_roe_pct: Decimal
    sl_net_roe_pct: Decimal  # ≤ tp
    min_conviction: float
    notes: tuple[str, ...] = ()


class RulesError(ValueError):
    """A (leverage, target) pair the fixed rules refuse. The trader stays flat this round."""


def plan_for(
    rules: Rules, leverage: int, tp_net_roe_pct: int, sl_net_roe_pct: float, min_conviction: float
) -> Plan:
    """Validate and clamp a rulebook's numbers (rules 3, 4, 5 and the fee rule)."""
    if not 1 <= leverage <= rules.max_leverage:
        raise RulesError(f"leverage {leverage}x outside 1-{rules.max_leverage}x")
    if not rules.tp_min <= tp_net_roe_pct <= rules.tp_max:
        raise RulesError(f"take-profit {tp_net_roe_pct}% outside {rules.tp_min}-{rules.tp_max}%")
    tp = Decimal(tp_net_roe_pct)
    fees = round_trip_fee_pct(rules, leverage)
    if fees > tp * rules.fee_share_max:
        raise RulesError(f"fees {fees}% of margin at {leverage}x exceed half the {tp}% target")
    notes: list[str] = []
    sl = Decimal(str(sl_net_roe_pct))
    if sl > tp:
        notes.append(f"stop clamped from {sl}% to the {tp}% take")
        sl = tp
    if sl <= fees:
        # A stop inside the fee band would sit on the wrong side of the entry.
        raise RulesError(f"stop {sl}% net is inside the {fees}% fee band at {leverage}x")
    conviction = max(float(min_conviction), rules.min_conviction_floor)
    if conviction != min_conviction:
        notes.append(f"minimum conviction raised to the floor {conviction}")
    return Plan(leverage, tp, sl, conviction, tuple(notes))


def tp_price(rules: Rules, plan: Plan, entry: Decimal, side: str, tick: Decimal) -> Decimal:
    """The take-profit level: the price move that nets the target, rounded to the tick on
    the far side so the fill nets at least the target."""
    move = price_move_pct(rules, plan.tp_net_roe_pct, plan.leverage) / 100
    raw = entry * (1 + move) if side == "long" else entry * (1 - move)
    return _tick(raw, tick, ROUND_UP if side == "long" else ROUND_DOWN)


def sl_price(rules: Rules, plan: Plan, entry: Decimal, side: str, tick: Decimal) -> Decimal:
    """The stop level: a net loss of ``sl`` on margin means a gross loss of ``sl − fees``,
    rounded to the tick on the near side so the loss never exceeds the stop."""
    gross = plan.sl_net_roe_pct - round_trip_fee_pct(rules, plan.leverage)
    move = gross / plan.leverage / 100
    raw = entry * (1 - move) if side == "long" else entry * (1 + move)
    return _tick(raw, tick, ROUND_UP if side == "long" else ROUND_DOWN)


def _tick(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (price / tick).quantize(Decimal(1), rounding=rounding) * tick


def is_loss(net_roe_pct: Decimal) -> bool:
    """Rule 6: any trade that closes at net ROE ≤ 0."""
    return net_roe_pct <= LOSS_NET_ROE


def is_benched(losses: int, rules: Rules) -> bool:
    """Rule 7: two losses in a round."""
    return losses >= rules.losses_to_bench


def round_over(benched: dict[str, bool]) -> bool:
    """Rule 8: the round resets when every trader is benched."""
    return bool(benched) and all(benched.values())


def qty_for(
    margin: Decimal, leverage: int, price: Decimal, lot: Decimal, min_qty: Decimal
) -> Decimal:
    """Contracts for ``margin`` at ``leverage``, rounded down to the lot; 0 when below the
    minimum order size."""
    if price <= 0 or margin <= 0:
        return Decimal(0)
    raw = margin * leverage / price
    qty = (raw / lot).quantize(Decimal(1), rounding=ROUND_DOWN) * lot
    return qty if qty >= min_qty else Decimal(0)


def net_roe_pct(pnl_usdt: Decimal, margin_usdt: Decimal) -> Decimal:
    """Return on the margin put in, as a percentage; ``pnl_usdt`` already net of fees."""
    if margin_usdt <= 0:
        return Decimal(0)
    return (pnl_usdt / margin_usdt * 100).quantize(Decimal("0.01"))
