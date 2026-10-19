"""The DEX mirror process: `python -m app.dex.main`. Idles while dex.enabled is false."""

from __future__ import annotations

import asyncio
import logging
import sys
from decimal import Decimal

from app.config import get_config, get_secrets
from app.dex.exchange import DexExchange, HyperliquidExchange
from app.dex.mirror import Mirror
from app.dex.paper import PaperDex
from app.dex.snapshot import publish

log = logging.getLogger("dex")


async def serve() -> None:
    cfg, secrets = get_config(), get_secrets()
    while not cfg.dex.enabled:
        log.info("dex: disabled in config; checking again in an hour")
        await asyncio.sleep(3600)
        get_config.cache_clear()
        cfg = get_config()
    mode = cfg.dex.mode
    testnet = mode != "live"
    address = secrets.dex_address
    key = secrets.dex_private_key.get_secret_value() or None
    venue = HyperliquidExchange(
        address=address, private_key=key if mode != "paper" else None, testnet=testnet
    )
    ex: DexExchange
    if mode == "paper":
        ex = PaperDex(
            equity=Decimal(cfg.dex.paper_equity),
            taker_fee_pct=cfg.dex.taker_fee_pct,
            prices=venue,
        )
    else:
        if not key or not address:
            raise SystemExit("dex: testnet and live need DEX_ADDRESS and DEX_PRIVATE_KEY in .env")
        ex = venue
    mirror = Mirror(cfg, ex, mode)
    await mirror.start()
    log.info("dex: %s on %s, following the %s book", mode.upper(), venue.url, cfg.dex.follow_track)
    last_publish = 0.0
    while True:
        loop = asyncio.get_event_loop()
        try:
            await mirror.tick()
        except Exception:
            log.exception("dex: tick failed")
        if loop.time() - last_publish >= cfg.dex.publish_seconds:
            try:
                await publish(cfg, ex, mode, address)
            except Exception:
                log.exception("dex: publish failed")
            last_publish = loop.time()
        await asyncio.sleep(cfg.dex.tick_seconds)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
