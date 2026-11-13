"""The slow book's signal. Per asset, one vote per lookback: is the last daily close above
or below the close that many days earlier. The majority decides the side. The position
is a share of the asset's slot, scaled so the asset contributes about ``vol_target``
annualised volatility and never more than the whole slot.

Pure functions on closed daily candles, oldest first; the same arithmetic as the tables in
docs/RETHINK_2026-11.md (scripts/review_history.py), where the rule was up in every
calendar year from 2018 to 2026.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

LOOKBACKS = (14, 28, 56)
VOL_WINDOW = 20
DAYS_PER_YEAR = 365


@dataclass(frozen=True)
class Signal:
    votes: tuple[int, ...]  # +1, -1 or 0 per lookback, in the order given
    side: str  # long | short | flat: what the majority says
    vol: float | None  # annualised, from the last ``vol_window`` daily returns
    weight: float  # share of the asset's slot, 0 to 1
    close: float  # the last closed daily close, the price the signal rests on


def vote(closes: Sequence[float], lookback: int) -> int:
    """+1 when the last close is above the close ``lookback`` days earlier, -1 below."""
    now, then = closes[-1], closes[-1 - lookback]
    return (now > then) - (now < then)


def annual_vol(closes: Sequence[float], window: int = VOL_WINDOW) -> float | None:
    """The sample standard deviation of the last ``window`` daily returns, annualised."""
    if len(closes) < window + 1:
        return None
    tail = closes[-window - 1 :]
    rets = [b / a - 1 for a, b in zip(tail, tail[1:], strict=False) if a > 0]
    if len(rets) < window:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(DAYS_PER_YEAR)


def side_of(votes: Sequence[int]) -> str:
    total = sum(votes)
    return "long" if total > 0 else "short" if total < 0 else "flat"


def signal(
    closes: Sequence[float],
    *,
    lookbacks: Sequence[int] = LOOKBACKS,
    vol_target: float = 0.25,
    vol_window: int = VOL_WINDOW,
) -> Signal | None:
    """The signal on the last close, or None when the history is too short to vote."""
    if len(closes) < max(max(lookbacks), vol_window) + 1 or closes[-1] <= 0:
        return None
    votes = tuple(vote(closes, lb) for lb in lookbacks)
    vol = annual_vol(closes, vol_window)
    weight = min(1.0, vol_target / vol) if vol and vol > 0 else 0.0
    return Signal(votes=votes, side=side_of(votes), vol=vol, weight=weight, close=closes[-1])
