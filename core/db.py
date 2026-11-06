"""PostgreSQL engine and sessions (SQLAlchemy 2)."""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import settings


@lru_cache
def engine() -> Engine:
    return create_engine(settings().database_url, pool_pre_ping=True)


@contextmanager
def session() -> Iterator[Session]:
    """Session committed on success, rolled back on error."""
    with sessionmaker(engine(), expire_on_commit=False)() as s:
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
