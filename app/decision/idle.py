"""Wait until no decision cycle is running (used by scripts/deploy.sh before a restart).

A deploy on 2026-10-11 restarted the scheduler one minute into a triggered cycle and the
cycle was lost. ``python -m app.decision.idle`` returns 0 once no cycle is running, or 1
after ``--max-wait`` seconds (the deploy then stops instead of interrupting it). It also
holds back when a scheduled cycle is about to start.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import get_config
from app.db.models import Cycle
from app.db.session import new_session

STALE = timedelta(minutes=20)  # a "running" row older than this is a dead cycle
LEAD = timedelta(seconds=90)  # do not restart this close before a scheduled cycle


def busy(now: datetime | None = None) -> str | None:
    """Why a restart should wait, or None when it is safe."""
    now = now or datetime.now(UTC)
    with new_session() as s:
        running = s.scalars(
            select(Cycle).where(Cycle.status == "running", Cycle.started_at >= now - STALE)
        ).first()
    if running is not None:
        return f"cycle {running.id} is running since {running.started_at:%H:%M:%S} UTC"
    step = max(1, get_config().trading.cycle_hours)
    nxt = now.replace(minute=2, second=0, microsecond=0)
    nxt = nxt.replace(hour=(nxt.hour // step) * step)
    while nxt <= now:
        nxt += timedelta(hours=step)
    if nxt - now <= LEAD:
        return f"the scheduled cycle starts at {nxt:%H:%M} UTC"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-wait", type=int, default=900)
    args = parser.parse_args(argv)
    deadline = time.monotonic() + args.max_wait
    while True:
        reason = busy()
        if reason is None:
            return 0
        if time.monotonic() >= deadline:
            print(f"still busy after {args.max_wait} s: {reason}")
            return 1
        print(f"waiting: {reason}", flush=True)
        time.sleep(15)


if __name__ == "__main__":
    sys.exit(main())
