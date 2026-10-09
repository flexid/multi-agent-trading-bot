"""Public WebSocket price feed (SPEC §9): best bid and ask per symbol, pushed.

Subscribes to ``orderbook.1.<symbol>`` on Bybit's public spot stream. Level 1 arrives as
a snapshot, then deltas, and Bybit re-sends the snapshot when nothing changed for a few
seconds, so a quote older than ``MAX_AGE_S`` means the stream is gone, not that the
market is quiet. The executor then falls back to the REST ticker for that pass.

No key, no orders: this module only reads public market data.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.execution.simulator import Quote

log = logging.getLogger("ws_feed")
WS_URL = "wss://stream.bybit.com/v5/public/spot"
TOPIC = "orderbook.1."
PING_S = 20
MAX_AGE_S = 10
BACKOFF_S = (1, 2, 5, 10, 30)


def _best(levels: Any) -> Decimal | None:
    """Price of the first level that still has size; None when the side is not in the
    message (a delta that did not touch it) or was only deleted."""
    for level in levels or []:
        try:
            price, size = Decimal(level[0]), Decimal(level[1])
        except (IndexError, InvalidOperation, TypeError):
            return None
        if size > 0:
            return price
    return None


class QuoteFeed:
    def __init__(self, symbols: list[str], url: str = WS_URL) -> None:
        self.symbols = symbols
        self.url = url
        self.quotes: dict[str, Quote] = {}
        self._task: asyncio.Task[None] | None = None

    def apply(self, message: str | bytes, now: datetime | None = None) -> str | None:
        """Fold one stream message into ``quotes``; returns the symbol it updated."""
        try:
            msg = json.loads(message)
        except ValueError:
            return None
        topic = msg.get("topic") if isinstance(msg, dict) else None
        if not isinstance(topic, str) or not topic.startswith(TOPIC):
            return None
        data = msg.get("data") or {}
        symbol = data.get("s") or topic[len(TOPIC) :]
        last = self.quotes.get(symbol)
        bid = _best(data.get("b")) or (last.bid if last and msg.get("type") != "snapshot" else None)
        ask = _best(data.get("a")) or (last.ask if last and msg.get("type") != "snapshot" else None)
        if bid is None or ask is None or bid <= 0 or ask < bid:
            return None  # one-sided or crossed: keep the previous quote and let it age
        self.quotes[symbol] = Quote(bid, ask, now or datetime.now(UTC))
        return str(symbol)

    def fresh(self, symbol: str, now: datetime, max_age_s: float = MAX_AGE_S) -> Quote | None:
        quote = self.quotes.get(symbol)
        if quote is None or (now - quote.ts).total_seconds() > max_age_s:
            return None
        return quote

    async def _session(self) -> None:
        import websockets

        async with websockets.connect(self.url, ping_interval=None, open_timeout=10) as ws:
            args = [f"{TOPIC}{s}" for s in self.symbols]
            await ws.send(json.dumps({"op": "subscribe", "args": args}))
            log.info("price stream connected: %d symbols", len(args))

            async def keepalive() -> None:
                while True:
                    await asyncio.sleep(PING_S)
                    await ws.send(json.dumps({"op": "ping"}))

            ping = asyncio.create_task(keepalive())
            try:
                async for message in ws:
                    self.apply(message)
            finally:
                ping.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await ping

    async def run(self) -> None:
        """Stay connected forever; reconnect with backoff. Never raises into the caller."""
        failures = 0
        while True:
            started = datetime.now(UTC)
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("price stream dropped: %s", exc)
            if (datetime.now(UTC) - started).total_seconds() > 60:
                failures = 0  # it held for a while: start the backoff over
            await asyncio.sleep(BACKOFF_S[min(failures, len(BACKOFF_S) - 1)])
            failures += 1

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="ws_feed")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
