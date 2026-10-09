"""Scheduler process: data fetches every 15 minutes (SPEC §5). Agent cycles arrive in M3+.

python -m app.scheduler            # run forever
python -m app.scheduler --once     # one pass of every job, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime

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
        "bybit_global.perps": fetch.make_fetch_perps(cfg),
        "bybit.margin_terms": fetch.make_fetch_margin_terms(cfg, secrets),
        "polymarket": fetch.make_fetch_polymarket(),
        "macro.fred": fetch.make_fetch_macro(secrets),
        "macro.gold_stables": fetch.make_fetch_gold_stables(),
        "crypto_native": fetch.make_fetch_crypto_native(secrets),  # every 15 min: dominance
    }


async def run_all(cfg: Config, secrets: Secrets) -> dict[str, bool]:
    results = {}
    for name, fetcher in jobs(cfg, secrets).items():
        results[name] = await fetch.run_job(name, fetcher)
    return results


async def cycle_job(cfg: Config, kind: str = "scheduled", trigger: str | None = None) -> None:
    from app.decision.cycle import run_cycle

    await run_cycle(cfg, kind=kind, trigger=trigger)


async def map_job() -> None:
    from app.agents.polymarket_map import map_unmapped

    n = await map_unmapped()
    log.info("polymarket mapper stored %d markets", n)


async def trigger_job(cfg: Config) -> None:
    from app import triggers
    from app.db.session import new_session

    with new_session() as session:
        reason = triggers.check(session, cfg)
    if reason:
        log.info("triggered cycle: %s", reason)
        await cycle_job(cfg, kind="triggered", trigger=reason)


async def serve(cfg: Config, secrets: Secrets) -> None:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        cycle_job,
        CronTrigger(hour=f"*/{cfg.trading.cycle_hours}", minute="2"),
        args=[cfg],
        id="cycle",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        map_job,
        CronTrigger(minute="1,16,31,46"),  # right after each Polymarket fetch, and at startup
        id="polymarket_map",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC),
    )
    scheduler.add_job(
        trigger_job,
        CronTrigger(minute="3,18,33,48"),
        args=[cfg],
        id="triggers",
        max_instances=1,
        coalesce=True,
    )
    for name, fetcher in jobs(cfg, secrets).items():
        # Macro data moves daily; the rest every 15 minutes, 20 s after the candle close.
        trigger = (
            CronTrigger(hour="*", minute="5", second="0")
            if name.startswith("macro.")
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
    scheduler.add_job(heartbeat_job, CronTrigger(second="30"), id="heartbeat", max_instances=1)
    scheduler.add_job(
        ops_job,
        CronTrigger(hour="0", minute="40"),
        args=["golive", cfg],
        id="golive",
        max_instances=1,
    )
    scheduler.add_job(
        ops_job,
        CronTrigger(hour="1", minute="10"),
        args=["backup", cfg],
        id="backup",
        max_instances=1,
    )
    scheduler.add_job(
        ops_job,
        CronTrigger(day="1,15", hour="1", minute="30"),
        args=["tuning", cfg],
        id="tuning",
        max_instances=1,
    )
    scheduler.add_job(
        ops_job,
        CronTrigger(day="1", hour="2", minute="0"),
        args=["costs", cfg],
        id="costs",
        max_instances=1,
    )
    scheduler.add_job(
        ops_job,
        CronTrigger(day_of_week="mon", hour="6", minute="0"),  # weekly memo, emailed
        args=["memo", cfg],
        id="memo",
        max_instances=1,
    )
    scheduler.add_job(
        publish_job,
        CronTrigger(minute=f"*/{cfg.site.snapshot_interval_minutes}", second="40"),
        id="site_snapshot",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("scheduler started with %d jobs", len(scheduler.get_jobs()))
    await run_all(cfg, secrets)  # fill the tables right away
    await asyncio.Event().wait()


async def publish_job() -> None:
    from app.site.publish import publish

    try:
        await asyncio.to_thread(publish)
    except Exception as exc:  # the site is never a reason to stop trading
        log.warning("site snapshot failed: %s", exc)


async def ops_job(kind: str, cfg: Config) -> None:
    from app import ops

    try:
        if kind == "golive":
            await asyncio.to_thread(ops.golive_check, cfg)
        elif kind == "backup":
            await asyncio.to_thread(ops.backup)
        elif kind == "tuning":
            await asyncio.to_thread(ops.tuning, cfg)
        elif kind == "costs":
            await asyncio.to_thread(ops.write_cost_report)
            log.info("cost report written to logs/costs.md")
        elif kind == "memo":
            from app import memo

            path = await memo.build(cfg)
            log.info("weekly memo written to %s and mailed", path)
    except Exception as exc:
        log.warning("ops job %s failed: %s", kind, exc)


async def heartbeat_job() -> None:
    from datetime import UTC, datetime

    from app.db.models import Heartbeat
    from app.db.session import new_session

    with new_session() as session:
        session.merge(Heartbeat(process="scheduler", ts=datetime.now(UTC), detail="ok"))
        session.commit()


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
