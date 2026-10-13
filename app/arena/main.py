"""The arena process: the minute loop and the read-only API in one service.

    python -m app.arena.main

Exits at once unless ``arena.enabled`` is true. With ``arena.live`` false every trader runs
on the paper twin over real prices; with it true each trader trades its own AI sub-account
with the key from .env, and the Bybit client is the only thing that places orders.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal

import uvicorn

from app.arena.api import build_app
from app.arena.candles import CandleBuffer
from app.arena.engine import Engine, Trader
from app.arena.exchange import Exchange, LinearExchange
from app.arena.paper import PaperExchange
from app.config import Config, Secrets, get_config, get_secrets
from app.execution.bybit_client import BybitClient

log = logging.getLogger("arena")
TICK_SECOND = 3  # seconds after the minute: the 1m candle has closed and is served
PAPER_EQUITY = Decimal(1000)


def build_traders(cfg: Config, secrets: Secrets, public: LinearExchange) -> dict[str, Trader]:
    a = cfg.arena
    out: dict[str, Trader] = {}
    for tid, tc in a.traders.items():
        ex: Exchange
        if a.live:
            key = getattr(secrets, f"competition_{tid}_api_key")
            secret = getattr(secrets, f"competition_{tid}_api_secret")
            if not key.get_secret_value():
                raise SystemExit(f"arena.live needs COMPETITION_{tid.upper()}_API_KEY in .env")
            client = BybitClient(
                secrets.bybit_base_url,
                key,
                secret,
                recv_window_ms=cfg.exchange.recv_window_ms,
                allow_orders=True,
            )
            ex = LinearExchange(client, a.market)
        else:
            ex = PaperExchange(
                a.market,
                equity=tc.allocation_usdt or PAPER_EQUITY,
                taker_fee_pct=a.taker_fee_pct,
                prices=public,
            )
        out[tid] = Trader(id=tid, model=tc.model, exchange=ex)
    return out


async def loop(engine: Engine) -> None:
    await engine.start()
    while True:
        now = datetime.now(UTC)
        wait = (60 - now.second + TICK_SECOND) % 60 or 60
        await asyncio.sleep(wait)
        try:
            await engine.tick(datetime.now(UTC))
        except Exception:  # the loop outlives any single minute's failure
            log.exception("arena: tick failed")


async def serve() -> None:
    cfg, secrets = get_config(), get_secrets()
    while not cfg.arena.enabled:
        log.info("arena: disabled in config; checking again in an hour")
        await asyncio.sleep(3600)
        get_config.cache_clear()
        cfg = get_config()
    public = LinearExchange(BybitClient(secrets.bybit_base_url), cfg.arena.market)
    traders = build_traders(cfg, secrets, public)
    engine = Engine(cfg.arena, traders, CandleBuffer(public))
    log.info(
        "arena: %s, %s, traders %s",
        "LIVE" if cfg.arena.live else "paper",
        cfg.arena.market,
        ", ".join(f"{t.id} ({t.model})" for t in traders.values()),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(engine), host="0.0.0.0", port=cfg.arena.api_port, log_level="warning"
        )
    )
    await asyncio.gather(server.serve(), loop(engine))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
