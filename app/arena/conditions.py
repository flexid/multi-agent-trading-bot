"""Structured rulebook conditions, evaluated exactly in code (2026-10-15).

Jev could not verify an author's prose against candles (five fully met setups on
2026-10-14, all judged "none" at 92-97%). The authors now write each condition in a small
form code can check: a timeframe, a field of a candle, a comparison, and a reference that
is a number, another candle's field, or an aggregate over a lookback. Candle index 0 is the
most recent closed candle, 1 the one before, and so on.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Timeframe = Literal["1m", "5m", "15m", "1h"]
Fld = Literal["open", "high", "low", "close", "volume", "range", "body"]
Op = Literal[">", "<", ">=", "<="]
Agg = Literal["max", "min", "avg"]
IDX = {"open": 1, "high": 2, "low": 3, "close": 4, "volume": 5}


class Ref(BaseModel):
    """What a candle field is compared with: a number, another candle's field, or the
    max/min/avg of a field over ``lookback`` candles starting at ``index``."""

    model_config = ConfigDict(extra="forbid")

    value: float | None = None
    timeframe: Timeframe | None = None
    field: Fld | None = None
    index: int = Field(default=1, ge=0, le=200)
    agg: Agg | None = None
    lookback: int = Field(default=1, ge=1, le=100)
    scale: float = Field(default=1.0, gt=0)  # multiply the reference: 1.5 = "1.5 × the average"


class Condition(BaseModel):
    """``left`` (timeframe, field, index) ``op`` ``ref``; ``pct``: compare the percentage
    difference ``(left / ref − 1) × 100`` with ``value`` instead."""

    model_config = ConfigDict(extra="forbid")

    timeframe: Timeframe
    field: Fld
    index: int = Field(default=0, ge=0, le=200)
    op: Op
    ref: Ref
    pct: bool = False
    note: str | None = Field(default=None, max_length=120)


Frames = dict[str, list[list[float]]]


def _field(row: list[float], fld: str) -> float:
    if fld == "range":
        return row[2] - row[3]
    if fld == "body":
        return abs(row[4] - row[1])
    return row[IDX[fld]]


def _candle(frames: Frames, tf: str, index: int) -> list[float] | None:
    rows = frames.get(tf, [])
    return rows[-1 - index] if index < len(rows) else None


def _ref_value(frames: Frames, c: Condition) -> float | None:
    r = c.ref
    if r.value is not None and r.agg is None and r.field is None:
        return r.value * r.scale
    tf, fld = r.timeframe or c.timeframe, r.field or c.field
    if r.agg is None:
        row = _candle(frames, tf, r.index)
        return None if row is None else _field(row, fld) * r.scale
    rows = frames.get(tf, [])
    start = len(rows) - r.index
    window = rows[max(0, start - r.lookback) : start]
    if len(window) < r.lookback:
        return None
    vals = [_field(x, fld) for x in window]
    agg = max(vals) if r.agg == "max" else min(vals) if r.agg == "min" else sum(vals) / len(vals)
    return agg * r.scale


def check(frames: Frames, c: Condition) -> bool | None:
    """True, False, or None when the candles do not reach far enough."""
    row = _candle(frames, c.timeframe, c.index)
    ref = _ref_value(frames, c)
    if row is None or ref is None:
        return None
    left = _field(row, c.field)
    if c.pct:
        if ref == 0:
            return None
        left, ref = (left / ref - 1) * 100, float(c.ref.value or 0)
    return {">": left > ref, "<": left < ref, ">=": left >= ref, "<=": left <= ref}[c.op]


def evaluate(frames: Frames, conditions: list[Condition]) -> dict[str, Any]:
    """All conditions; ``met`` only when every one is True."""
    results = [check(frames, c) for c in conditions]
    return {
        "met": bool(conditions) and all(r is True for r in results),
        "count_met": sum(1 for r in results if r is True),
        "count": len(conditions),
        "results": results,
    }


def describe(c: Condition) -> str:
    """One line a reader can check: '5m close[0] > max high over 8 from [1]'."""
    r = c.ref
    if r.value is not None and r.agg is None and r.field is None:
        right = f"{r.value:g}{'%' if c.pct else ''}"
    elif r.agg is None:
        right = f"{r.timeframe or c.timeframe} {r.field or c.field}[{r.index}]"
    else:
        right = (
            f"{r.agg} {r.field or c.field} over {r.lookback} {r.timeframe or c.timeframe} "
            f"from [{r.index}]"
        )
    if r.scale != 1 and not (r.value is not None and r.agg is None and r.field is None):
        right = f"{r.scale:g} × {right}"
    left = f"{c.timeframe} {c.field}[{c.index}]"
    return f"{left} {'% vs ' if c.pct else ''}{c.op} {right}" if c.pct else f"{left} {c.op} {right}"
