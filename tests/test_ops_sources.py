"""The daily look at inputs that keep failing (app/ops.py). 2026-11-13: X had refused
every read with "402 Payment Required" for a week and nothing told the owner."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.db.models import DataSource
from app.db.session import new_session
from app.ops import describe_failing, failing_sources, sources_alert
from tests.test_data_layer import needs_db

NOW = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)


@pytest.fixture
def db() -> Iterator[None]:
    def clean() -> None:
        with new_session() as s:
            s.execute(delete(DataSource).where(DataSource.name.like("t.%")))
            s.commit()

    clean()
    yield
    clean()


def source(name: str, *, ok_hours_ago: float | None, errors: int, error_hours_ago: float) -> None:
    with new_session() as s:
        s.add(
            DataSource(
                name=name,
                last_success_at=NOW - timedelta(hours=ok_hours_ago) if ok_hours_ago else None,
                last_error_at=NOW - timedelta(hours=error_hours_ago),
                last_error="/tweets/search/recent: HTTP 402: Payment Required",
                consecutive_errors=errors,
            )
        )
        s.commit()


@needs_db
def test_an_input_failing_for_hours_is_reported_once_a_day_and_grouped(
    db: None, _no_mail: list[tuple[str, str]]
) -> None:
    for n in range(3):  # three numbered queries of one source: one line
        source(f"t.curated.q{n}", ok_hours_ago=180 + n, errors=3632, error_hours_ago=0.05)
    source("t.hiccup", ok_hours_ago=1, errors=5, error_hours_ago=0.1)  # worked an hour ago
    source("t.retired", ok_hours_ago=400, errors=0, error_hours_ago=400)  # nobody asks any more
    source("t.gone", ok_hours_ago=900, errors=40, error_hours_ago=72)  # stopped trying
    source("t.never", ok_hours_ago=None, errors=9, error_hours_ago=0.2)  # has never worked
    with new_session() as s:
        failing = [f for f in failing_sources(s, NOW) if f["source"].startswith("t.")]
    assert [f["source"] for f in failing] == ["t.curated", "t.never"]
    curated, never = failing
    assert curated["parts"] == 3 and curated["errors_in_a_row"] == 3632
    assert curated["since"] == "2026-11-06 01:00"  # the oldest of the three
    lines = describe_failing(failing)
    assert lines[0] == (
        "t.curated (3 queries): no success since 2026-11-06 01:00 UTC, 3632 errors in a row; "
        "last answer: /tweets/search/recent: HTTP 402: Payment Required"
    )
    assert lines[1].startswith("t.never: never succeeded, 9 errors in a row")
    reported = sources_alert(NOW)
    assert {f["source"] for f in reported} >= {"t.curated", "t.never"}
    (subject, body) = _no_mail[-1]
    assert subject == "dorkbot: an input has been failing for hours"
    assert "t.curated (3 queries)" in body and "402: Payment Required" in body
    assert "t.hiccup" not in body and "t.retired" not in body and "t.gone" not in body


@needs_db
def test_no_mail_when_every_input_answers(db: None, _no_mail: list[tuple[str, str]]) -> None:
    with new_session() as s:
        s.execute(delete(DataSource))  # the test database's own leftovers
        s.commit()
    source("t.hiccup", ok_hours_ago=1, errors=5, error_hours_ago=0.1)
    assert sources_alert(NOW) == [] and _no_mail == []
