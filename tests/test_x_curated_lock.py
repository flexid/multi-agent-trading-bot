"""Two curated X fetches in one process take turns. 2026-11-13: the three-minute poll
and a cycle's X agent started in the same second, each wrote the same marker rows in its
own session, and the scheduler stood still until it was restarted."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select

from app.agents import x_sentiment as xs
from app.config import get_config
from app.db.models import DataSource
from app.db.session import new_session
from tests.test_data_layer import needs_db


class Overlap(BaseException):
    """Not an Exception: the fetch must not swallow it as a refused query."""


class SlowX:
    """A client whose every search yields to the event loop, as a real request does."""

    def __init__(self) -> None:
        self.inflight = 0
        self.calls = 0

    async def search_recent(self, query: str, **kwargs: Any) -> list[Any]:
        self.inflight += 1
        self.calls += 1
        try:
            if self.inflight > 1:
                raise Overlap("two curated fetches are talking to X at once")
            for _ in range(5):
                await asyncio.sleep(0)
            return []
        finally:
            self.inflight -= 1


@needs_db
async def test_two_curated_fetches_take_turns_and_both_finish() -> None:
    cfg = get_config()
    now = datetime.now(UTC)
    client = SlowX()

    def clean() -> None:
        with new_session() as s:
            s.execute(delete(DataSource).where(DataSource.name.like("x.curated.q%")))
            s.commit()

    clean()
    try:
        with new_session() as a, new_session() as b:
            results = await asyncio.wait_for(
                asyncio.gather(
                    xs.fetch_curated(a, cfg, client, now, 100),  # type: ignore[arg-type]
                    xs.fetch_curated(b, cfg, client, now, 100),  # type: ignore[arg-type]
                ),
                timeout=20,
            )
        assert [r[0] for r in results] == [0, 0]
        queries = len(
            xs.curated_queries(xs.curated_handles(cfg), cfg.trading.assets, cfg.x.query_max_chars)
        )
        assert queries >= 1 and client.calls == 2 * queries
        with new_session() as s:
            markers = s.scalars(
                select(DataSource).where(DataSource.name.like("x.curated.q%"))
            ).all()
        assert len(markers) == queries and all(m.consecutive_errors == 0 for m in markers)
    finally:
        clean()
