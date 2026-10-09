"""Scheduler process: data fetches every 15 minutes (SPEC §5). Agent cycles arrive in M3+.

python -m app.scheduler            # run forever
python -m app.scheduler --once     # one pass of every job, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Config, Secrets, get_config, get_secrets
from app.data import fetch

log = logging.getLogger("scheduler")


def jobs(cfg: Config, secrets: Secrets) -> dict[str, fetch.Fetcher]:
    return {
        "bybit.candles": fetch.make_fetch_candles(cfg, secrets),
        "bybit.books": fetch.make_fetch_books(cfg, secrets),
        "bybit.account": fetch.make_fetch_account(cfg, secrets),
        "polymarket": fetch.make_fetch_polymarket(),
        "macro.fred": fetch.make_fetch_macro(secrets),
    }


async def run_all(cfg: Config, secrets: Secrets) -> dict[str, bool]:
    results = {}
    for name, fetcher in jobs(cfg, secrets).items():
        results[name] = await fetch.run_job(name, fetcher)
    return results


async def serve(cfg: Config, secrets: Secrets) -> None:
    scheduler = AsyncIOScheduler(timezone="UTC")
    for name, fetcher in jobs(cfg, secrets).items():
        # Macro data moves daily; the rest every 15 minutes, 20 s after the candle close.
        trigger = (
            CronTrigger(hour="*", minute="5", second="0")
            if name == "macro.fred"
            else CronTrigger(minute="0,15,30,45", second="20")
        )
        scheduler.add_job(
            fetch.run_job,
            trigger,
            args=[name, fetcher],
            id=name,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=120,
        )
    scheduler.start()
    log.info("scheduler started with %d jobs", len(scheduler.get_jobs()))
    await run_all(cfg, secrets)  # fill the tables right away
    await asyncio.Event().wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--once", action="store_true", help="run every job once and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # httpx logs full request URLs at INFO; FRED carries its key in the query string.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg, secrets = get_config(), get_secrets()
    if args.once:
        results = asyncio.run(run_all(cfg, secrets))
        for name, ok in results.items():
            print(f"{'ok  ' if ok else 'FAIL'}  {name}")
        return 0 if all(results.values()) else 1
    asyncio.run(serve(cfg, secrets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
