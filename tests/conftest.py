"""Route every DB-backed test at TEST_DATABASE_URL (migrated on first use), never at the
development database. Without it the DB tests skip."""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import session as db_session


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> None:
    url = config.get_secrets().test_database_url.get_secret_value()
    if not url:
        return
    from alembic.config import Config as AlembicConfig

    from alembic import command

    secrets = config.get_secrets()
    object.__setattr__(secrets, "database_url", SecretStr(url))
    db_session.get_engine.cache_clear()
    db_session.session_factory.cache_clear()
    try:
        command.upgrade(AlembicConfig("alembic.ini"), "head")
    except Exception as exc:  # unreachable test DB: the DB tests skip via needs_db
        print(f"test database unavailable: {exc}")
        return
    engine = create_engine(url, pool_pre_ping=True)
    db_session.get_engine.cache_clear()
    db_session.session_factory.cache_clear()
    db_session.get_engine = lambda: engine  # type: ignore[assignment]
    db_session.session_factory = lambda: sessionmaker(engine, expire_on_commit=False)  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _no_mail(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Tests never email the owner (the executor's kill alert did, from the test suite)."""
    from app.admin import notify

    sent: list[tuple[str, str]] = []

    def fake_send(subject: str, body: str, *args: object, **kwargs: object) -> bool:
        sent.append((subject, body))
        return True

    monkeypatch.setattr(notify, "send", fake_send)
    return sent
