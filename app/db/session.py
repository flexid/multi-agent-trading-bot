"""Engine and session factory. Sync SQLAlchemy; the fetch volume does not need async I/O."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_secrets

LOCK_TIMEOUT_MS = 20_000


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = get_secrets().database_url.get_secret_value()
    if not url:
        raise RuntimeError("DATABASE_URL is empty")
    # A statement that waits on a row lock gives up after LOCK_TIMEOUT_MS and raises. The
    # database calls are synchronous on each process's event loop, so a wait on a lock
    # held by another task of the same process can never end by itself (the scheduler,
    # 2026-11-13); an error that is logged and retried beats a process that stands still.
    return create_engine(
        url, pool_pre_ping=True, connect_args={"options": f"-c lock_timeout={LOCK_TIMEOUT_MS}"}
    )


@lru_cache(maxsize=1)
def session_factory() -> sessionmaker[Session]:
    return sessionmaker(get_engine(), expire_on_commit=False)


def new_session() -> Session:
    return session_factory()()
