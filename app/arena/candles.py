"""The only input: closed OHLCV candles on 1m, 5m, 15m and 1h, the last 150 of each,
refreshed from the exchange after every 1-minute close and serialized compactly for Jev."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.arena.exchange import Exchange

INTERVALS = {"1m": "1", "5m": "5", "15m": "15", "1h": "60"}
KEEP = 150  # the 15m frame keeps a day for the authors (96); the rest is plenty
# What Jev sees per timeframe: its window is small (a state of 600 full-precision candles
# was refused on the first run, 2026-10-13), so fewer candles, prices rounded, volumes as
# integers and times as minutes before now.
JEV_COUNT = {"1m": 120, "5m": 72, "15m": 64, "1h": 72}
PRICE_DECIMALS = 1


class CandleBuffer:
    def __init__(self, exchange: Exchange) -> None:
        self._ex = exchange
        self.frames: dict[str, list[list[float]]] = {k: [] for k in INTERVALS}
        self.last_1m_ts: float | None = None

    async def refresh(self) -> bool:
        """Fetch every timeframe. Returns True when a new 1-minute candle closed."""
        for name, code in INTERVALS.items():
            rows = await self._ex.klines(code, KEEP + 1)
            if rows:
                self.frames[name] = rows[-KEEP:]
        last = self.frames["1m"][-1][0] if self.frames["1m"] else None
        new = last is not None and last != self.last_1m_ts
        self.last_1m_ts = last
        return new

    @property
    def last_1m(self) -> list[float] | None:
        return self.frames["1m"][-1] if self.frames["1m"] else None

    def last_close(self) -> Decimal | None:
        row = self.last_1m
        return Decimal(str(row[4])) if row else None

    def state(self, position: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
        """What Jev sees: per timeframe the last candles as
        [minutes_before_now, open, high, low, close, volume], oldest first, plus the position."""
        now = now or datetime.now(UTC)
        now_ms = now.timestamp() * 1000
        return {
            "now": now.isoformat(timespec="minutes"),
            "facts": facts(self.frames),
            "candle_fields": "[minutes_before_now, open, high, low, close, volume], oldest first",
            "candles": {
                k: [
                    [
                        int(round((now_ms - r[0]) / 60000)),
                        round(r[1], PRICE_DECIMALS),
                        round(r[2], PRICE_DECIMALS),
                        round(r[3], PRICE_DECIMALS),
                        round(r[4], PRICE_DECIMALS),
                        int(r[5]),
                    ]
                    for r in v[-JEV_COUNT[k] :]
                ]
                for k, v in self.frames.items()
            },
            "position": position,
        }


# --- facts: what code can check exactly, so Jev judges rather than computes ---------------

LOOKBACKS = (5, 10, 20)


def _pct(a: float, b: float) -> float:
    return round((a / b - 1) * 100, 3) if b else 0.0


def frame_facts(rows: list[list[float]]) -> dict[str, Any]:
    """Observations on one timeframe, computed exactly: the last candle, where its close sits
    against the prior 5, 10 and 20 candles, volume against the average, runs of closes,
    ranges. The probe of 2026-10-14 showed Jev cannot verify these from the arrays."""
    if len(rows) < 25:
        return {"candles": len(rows)}
    last, prev = rows[-1], rows[-2]
    o, h, lo, c, v = last[1], last[2], last[3], last[4], last[5]
    out: dict[str, Any] = {
        "last": {
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "direction": "up" if c > o else "down" if c < o else "flat",
            "range_pct": _pct(h, lo),
            "close_vs_previous_close_pct": _pct(c, prev[4]),
            "closed_above_previous_high": c > prev[2],
            "closed_below_previous_low": c < prev[3],
        }
    }
    for n in LOOKBACKS:
        prior = rows[-1 - n : -1]
        hi = max(r[2] for r in prior)
        lw = min(r[3] for r in prior)
        out[f"vs_prior_{n}"] = {
            "highest_high": hi,
            "lowest_low": lw,
            "close_position": "above the highest high"
            if c > hi
            else "below the lowest low"
            if c < lw
            else "inside the range",
            "distance_to_highest_high_pct": _pct(c, hi),
            "distance_to_lowest_low_pct": _pct(c, lw),
            "range_pct": _pct(hi, lw),
        }
    vols = [r[5] for r in rows[-21:-1]]
    out["volume_vs_avg_20"] = round(v / (sum(vols) / len(vols)), 2) if vols and sum(vols) else None
    up = down = 0
    for r in reversed(rows):
        if r[4] > r[1]:
            if down:
                break
            up += 1
        elif r[4] < r[1]:
            if up:
                break
            down += 1
        else:
            break
    out["consecutive_up_closes"] = up
    out["consecutive_down_closes"] = down
    closes = [r[4] for r in rows[-4:]]
    out["last_3_closes"] = (
        "each higher than the one before"
        if closes[1] < closes[2] < closes[3]
        else "each lower than the one before"
        if closes[1] > closes[2] > closes[3]
        else "mixed"
    )
    ranges = [_pct(r[2], r[3]) for r in rows[-20:]]
    out["avg_range_pct_last_20"] = round(sum(ranges) / len(ranges), 3)
    out["max_range_pct_last_10"] = round(max(ranges[-10:]), 3)
    return out


def breakout_facts(frames: dict[str, list[list[float]]]) -> dict[str, Any]:
    """The most recent 15m close outside its prior 20-candle range, how many 15m candles
    ago, with its volume ratio, and what the 1m candles did since: the deepest pullback
    from that close and whether the last 1m candle closed back beyond the previous one."""
    f15, f1 = frames.get("15m", []), frames.get("1m", [])
    if len(f15) < 25:
        return {}
    found: dict[str, Any] = {}
    for back in range(1, min(16, len(f15) - 20)):
        row = f15[-back]
        prior = f15[-back - 20 : -back]
        hi, lw = max(r[2] for r in prior), min(r[3] for r in prior)
        if row[4] > hi or row[4] < lw:
            vols = [r[5] for r in prior]
            found = {
                "direction": "up" if row[4] > hi else "down",
                "candles_ago": back - 1,
                "close": row[4],
                "range_pct": _pct(row[2], row[3]),
                "volume_vs_avg_20": round(row[5] / (sum(vols) / len(vols)), 2)
                if sum(vols)
                else None,
                "prior_20_range_pct": _pct(hi, lw),
            }
            break
    if not found or not f1:
        return {"last_15m_breakout": None}
    minutes = (found["candles_ago"] + 1) * 15
    since = f1[-minutes:] if minutes <= len(f1) else f1
    bo = found["close"]
    if found["direction"] == "up":
        deepest = min(r[3] for r in since)
        found["deepest_pullback_pct"] = round((1 - deepest / bo) * 100, 3)
    else:
        highest = max(r[2] for r in since)
        found["deepest_bounce_pct"] = round((highest / bo - 1) * 100, 3)
    found["last_1m_close_vs_breakout_close_pct"] = _pct(f1[-1][4], bo)
    return {"last_15m_breakout": found}


def facts(frames: dict[str, list[list[float]]]) -> dict[str, Any]:
    out: dict[str, Any] = {k: frame_facts(v) for k, v in frames.items()}
    out.update(breakout_facts(frames))
    return out
