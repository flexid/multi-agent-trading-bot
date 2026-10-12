"""An extra decision cycle right after aggressive mode banks its day goal (owner 2026-10-12).

The executor closes every trade on a ledger when the goal is reached and leaves a request
(``control_requests`` kind ``cycle``). The scheduler asks here every half minute whether to
act on it: the managers then look at the market again at once instead of the book sitting
in cash until the next regular cycle. No request is carried over: it runs now or not at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config
from app.db.models import ControlRequest, Cycle, RiskState

KIND = "cycle"  # control_requests.kind
CYCLE_KIND = "goal"  # cycles.kind: not "triggered", so it does not use up the trigger cap
RUNNING_STALE = timedelta(minutes=20)  # a "running" row older than this is a dead cycle


def next_scheduled(now: datetime, cycle_hours: int) -> datetime:
    step = max(1, cycle_hours)
    nxt = now.replace(minute=2, second=0, microsecond=0)
    nxt = nxt.replace(hour=(nxt.hour // step) * step)
    while nxt <= now:
        nxt += timedelta(hours=step)
    return nxt


def request(session: Session, now: datetime, track: str) -> None:
    """Called by the executor when a ledger banked its goal. One open request is enough."""
    pending = session.scalars(
        select(ControlRequest).where(
            ControlRequest.kind == KIND, ControlRequest.applied_at.is_(None)
        )
    ).first()
    if pending is None:
        session.add(
            ControlRequest(
                ts=now, kind=KIND, source="day_goal", reason=f"day goal banked on {track}"
            )
        )


def claim(session: Session, cfg: Config, now: datetime) -> str | None:
    """Settle the open requests. Returns the trigger text when a cycle should start now."""
    pending = session.scalars(
        select(ControlRequest)
        .where(ControlRequest.kind == KIND, ControlRequest.applied_at.is_(None))
        .order_by(ControlRequest.id)
    ).all()
    if not pending:
        return None
    state = session.get(RiskState, 1)
    nxt = next_scheduled(now, cfg.trading.cycle_hours)
    gap = timedelta(minutes=cfg.aggressive.goal_cycle_min_gap_minutes)
    running = session.scalars(
        select(Cycle).where(Cycle.status == "running", Cycle.started_at >= now - RUNNING_STALE)
    ).first()
    if state is None or not state.aggressive:
        verdict = "skipped: aggressive mode is off"
    elif not cfg.aggressive.goal_cycle:
        verdict = "skipped: switched off in config"
    elif running is not None:
        verdict = f"skipped: cycle {running.id} is already running"
    elif nxt - now <= gap:
        verdict = f"skipped: the regular cycle starts at {nxt:%H:%M} UTC"
    else:
        verdict = "cycle started"
    for req in pending:
        req.applied_at, req.result = now, verdict
    session.commit()
    return pending[0].reason if verdict == "cycle started" else None
