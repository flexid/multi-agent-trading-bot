"""Engine and session factory. Sync SQLAlchemy; the fetch volume does not need async I/O."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_secrets


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = get_secrets().database_url.get_secret_value()
    if not url:
        raise RuntimeError("DATABASE_URL is empty")
    return create_engine(url, pool_pre_ping=True)


@lru_cache(maxsize=1)
def session_factory() -> sessionmaker[Session]:
    return sessionmaker(get_engine(), expire_on_commit=False)


def new_session() -> Session:
    return session_factory()()
