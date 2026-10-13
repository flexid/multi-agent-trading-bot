"""The only input: closed OHLCV candles on 1m, 5m, 15m and 1h, the last 150 of each,
refreshed from the exchange after every 1-minute close and serialized compactly for Jev."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.arena.exchange import Exchange

INTERVALS = {"1m": "1", "5m": "5", "15m": "15", "1h": "60"}
KEEP = 150
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
