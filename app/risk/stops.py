"""Stop placement against stop hunts (owner briefing 2026-10-10). Pure functions.

1. A stop never sits on a level: not on a round number, not on a recent swing low or
   high. When the PM's stop lands within ``buffer`` ATR of one, it moves beyond the level
   by that buffer (and again if that lands on the next level), capped at ``MAX_ATR`` from
   the entry so the stop stays a stop.
2. Two stops per trade: the soft stop (the placed one) triggers on a 15-minute candle
   close beyond it; the hard stop sits ``hard`` ATR further and triggers on touch. Sizing
   uses the hard stop, so a touch loses at most ``risk_per_trade``. The exchange-side
   backup stop sits beyond the hard stop (executor).
3. A stopped-out trade is a "wick-out" when price reached the original target within the
   planned holding time anyway. The rate feeds the buffer multiple at tuning time.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.decision.pm import Direction

MAX_ATR = Decimal(3)  # a stop further than this from the entry is no longer a stop
ROUND_GRID_PCT = Decimal("0.015")  # round numbers at roughly 1.5% of price
SWING_BARS = 2  # a swing low/high stands out against this many bars on each side
BUFFER_MIN, BUFFER_MAX, BUFFER_STEP = Decimal("0.25"), Decimal("1.5"), Decimal("0.25")
WICK_OUT_HIGH, WICK_OUT_LOW, WICK_OUT_MIN_TRADES = 0.40, 0.15, 20


def _nice(x: Decimal) -> Decimal:
    """The 1-2-5 step at or below ``x``."""
    if x <= 0:
        return Decimal(1)
    exp = math.floor(math.log10(float(x)))
    base = Decimal(10) ** exp
    for m in (5, 2, 1):
        if base * m <= x:
            return base * m
    return base


def round_levels(price: Decimal, lo: Decimal, hi: Decimal) -> list[Decimal]:
    """Round numbers between ``lo`` and ``hi`` on a grid of about 1.5% of price."""
    step = _nice(price * ROUND_GRID_PCT)
    first = (lo / step).to_integral_value(rounding="ROUND_CEILING") * step
    out = []
    x = first
    while x <= hi:
        out.append(x)
        x += step
    return out


def swing_levels(highs: Sequence[Decimal], lows: Sequence[Decimal]) -> list[Decimal]:
    """Pivot lows and highs of a candle series (oldest first)."""
    out = []
    n = len(highs)
    for i in range(SWING_BARS, n - SWING_BARS):
        window = range(i - SWING_BARS, i + SWING_BARS + 1)
        if all(lows[i] <= lows[j] for j in window):
            out.append(lows[i])
        if all(highs[i] >= highs[j] for j in window):
            out.append(highs[i])
    return out


@dataclass(frozen=True)
class Placement:
    soft: Decimal
    hard: Decimal
    moved: bool
    reason: str


def place_stop(
    direction: Direction,
    entry: Decimal,
    stop: Decimal,
    atr: Decimal | None,
    levels: Sequence[Decimal],
    *,
    buffer: Decimal = Decimal("0.5"),
    hard: Decimal = Decimal(1),
) -> Placement:
    """The soft and hard stop for a proposal. Without an ATR nothing moves and the hard
    stop equals the soft one (touch), as before this rule."""
    if atr is None or atr <= 0:
        return Placement(stop, stop, False, "no ATR")
    pad = buffer * atr
    soft = stop
    moved: list[str] = []
    for _ in range(3):  # beyond one level can be on the next
        if direction is Direction.LONG:
            near = [lv for lv in levels if lv <= entry and abs(lv - soft) <= pad]
            if not near:
                break
            lv = min(near)
            soft = lv - pad
        else:
            near = [lv for lv in levels if lv >= entry and abs(lv - soft) <= pad]
            if not near:
                break
            lv = max(near)
            soft = lv + pad
        moved.append(f"{lv:f}")
    limit = MAX_ATR * atr
    if abs(entry - soft) > limit:
        soft = entry - limit if direction is Direction.LONG else entry + limit
        moved.append("capped at 3 ATR")
    hard_stop = soft - hard * atr if direction is Direction.LONG else soft + hard * atr
    reason = (
        f"moved {stop:f} → {soft:f} past " + ", ".join(moved) if moved else "stop clear of levels"
    )
    return Placement(soft, hard_stop, bool(moved), reason)


def wick_out_for(
    direction: str, target: Decimal, highs: Sequence[Decimal], lows: Sequence[Decimal]
) -> bool:
    """Did price reach the target during the candles after the stop-out?"""
    if direction == "long":
        return any(h >= target for h in highs)
    return any(lo <= target for lo in lows)


def next_buffer(current: Decimal, wick_out_rate: float | None, stopped: int) -> Decimal:
    """The buffer multiple for the next window: up a step when many stops were wick-outs,
    down a step when few were; unchanged below the sample floor."""
    if wick_out_rate is None or stopped < WICK_OUT_MIN_TRADES:
        return current
    if wick_out_rate > WICK_OUT_HIGH:
        return min(BUFFER_MAX, current + BUFFER_STEP)
    if wick_out_rate < WICK_OUT_LOW:
        return max(BUFFER_MIN, current - BUFFER_STEP)
    return current
