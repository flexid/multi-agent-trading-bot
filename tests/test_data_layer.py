from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.data import fetch
from app.data.fred import FredClient
from app.db.models import DataSource
from app.execution.bybit_models import Kline


def test_kline_row_parses_to_utc_decimals() -> None:
    k = Kline.from_row(["1791500400000", "82000.1", "82100", "81900", "82050.5", "12.5", "1025000"])
    assert k.open_time == datetime(2026, 10, 8, 23, 0, tzinfo=UTC)
    assert k.close == Decimal("82050.5")
    assert k.turnover == Decimal("1025000")


def test_intervals_cover_spec() -> None:
    assert set(fetch.INTERVALS) == {"15", "60", "240", "D"}
    assert timedelta(minutes=30) == fetch.MAX_DATA_AGE


async def test_fred_skips_missing_values_and_keeps_key_out_of_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["series_id"] == "VIXCLS"
        return httpx.Response(
            200,
            json={
                "observations": [
                    {"date": "2026-10-06", "value": "18.4"},
                    {"date": "2026-10-07", "value": "."},
                ]
            },
        )

    async with FredClient(SecretStr("KEY"), transport=httpx.MockTransport(handler)) as fred:
        rows = await fred.observations("VIXCLS", datetime(2026, 10, 1).date())
    assert [(r.date.day, r.value) for r in rows] == [(6, Decimal("18.4"))]


# --- Postgres-backed tests: run only when the local database is up ----------


def _db_available() -> bool:
    try:
        from app.db.session import get_engine

        with get_engine().connect() as conn:
            conn.execute(text("select 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="Postgres not reachable")


@needs_db
async def test_run_job_records_success_and_failure() -> None:
    from app.db.session import new_session

    source = "test.source"

    async def good(session: object) -> int:
        return 3

    async def bad(session: object) -> int:
        raise RuntimeError("boom")

    assert await fetch.run_job(source, good) is True
    with new_session() as s:
        assert fetch.is_fresh(s, source)
        assert fetch.data_age(s, source) is not None

    assert await fetch.run_job(source, bad) is False
    with new_session() as s:
        state = s.get(DataSource, source)
        assert state is not None
        assert state.consecutive_errors == 1
        assert state.last_error is not None and "boom" in state.last_error
        assert fetch.is_fresh(s, source)  # last success still counts until it ages out
        s.execute(text("delete from fetch_runs where source = :s"), {"s": source})
        s.execute(text("delete from data_sources where name = :s"), {"s": source})
        s.commit()


@needs_db
def test_data_age_unknown_source_is_none() -> None:
    from app.db.session import new_session

    with new_session() as s:
        assert fetch.data_age(s, "never.fetched") is None
        assert not fetch.is_fresh(s, "never.fetched")
