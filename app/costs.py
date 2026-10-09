"""Running costs of the bot itself: LLM calls, X reads and posts, the server.

One place computes them, so the monthly report, the go-live check and both ramps agree
on what "net after all costs" means: trading P&L (already net of fees and borrow
interest) minus these. Costs are in USD and are set against USDT P&L one for one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import LLMCall, XPostOut, XPostRecord

X_READ_USD = Decimal("0.005")
X_POST_USD = Decimal("0.015")
SERVER_USD_PER_MONTH = Decimal(24)  # DigitalOcean 2 vCPU / 4 GB droplet
DAYS_PER_MONTH = Decimal("30.4375")


@dataclass(frozen=True)
class RunningCosts:
    llm: Decimal
    x_reads: int
    x_posts: int
    server: Decimal

    @property
    def x(self) -> Decimal:
        return self.x_reads * X_READ_USD + self.x_posts * X_POST_USD

    @property
    def total(self) -> Decimal:
        return self.llm + self.x + self.server


def server_cost(since: datetime, until: datetime) -> Decimal:
    days = Decimal(str(max(0.0, (until - since).total_seconds() / 86400)))
    return SERVER_USD_PER_MONTH * days / DAYS_PER_MONTH


def running_costs(session: Session, since: datetime, until: datetime) -> RunningCosts:
    """Everything the bot spent on itself in ``[since, until)``. Every LLM call counts,
    including the second-provider runs of shadow mode."""
    llm = session.execute(
        select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(
            LLMCall.ts >= since, LLMCall.ts < until
        )
    ).scalar_one()
    reads = session.execute(
        select(func.count())
        .select_from(XPostRecord)
        .where(XPostRecord.fetched_at >= since, XPostRecord.fetched_at < until)
    ).scalar_one()
    posts = session.execute(
        select(func.count())
        .select_from(XPostOut)
        .where(XPostOut.posted_at >= since, XPostOut.posted_at < until, XPostOut.dry_run.is_(False))
    ).scalar_one()
    return RunningCosts(
        llm=Decimal(str(llm or 0)),
        x_reads=int(reads),
        x_posts=int(posts),
        server=server_cost(since, until),
    )
