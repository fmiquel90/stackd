from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from functools import lru_cache
from typing import ClassVar

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, mapped_column
from sqlalchemy.orm.properties import MappedColumn

from app.config import get_settings

# Stable naming convention so Alembic autogenerate produces deterministic constraint names.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(AsyncAttrs, DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # All datetimes are timestamptz UTC (SPECS §1).
    type_annotation_map: ClassVar[dict] = {datetime: DateTime(timezone=True)}


def pk_uuid() -> MappedColumn[uuid.UUID]:
    # PG18 native uuidv7() default — temporal ordering preserved (SPECS §1).
    from sqlalchemy import text
    from sqlalchemy.dialects.postgresql import UUID as PGUUID

    return mapped_column(PGUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()"))


def created_at_col() -> MappedColumn[datetime]:
    return mapped_column(server_default=func.now())


def updated_at_col() -> MappedColumn[datetime]:
    return mapped_column(server_default=func.now(), onupdate=func.now())


def _server_settings() -> dict[str, str]:
    """Session GUCs of the API's connections (asyncpg `server_settings`, in ms). A leaked or stuck
    transaction is ended by the server instead of pinning a pooled connection and its row locks;
    a lock wait gives up instead of queueing forever. Alembic builds its own engine: a migration
    never inherits them."""
    s = get_settings()
    timeouts = {
        "idle_in_transaction_session_timeout": s.stackd_db_idle_in_transaction_timeout_seconds,
        "lock_timeout": s.stackd_db_lock_timeout_seconds,
        "statement_timeout": s.stackd_db_statement_timeout_seconds,
    }
    return {name: str(seconds * 1000) for name, seconds in timeouts.items() if seconds > 0}


# Engine/sessionmaker are built lazily on first use — importing this module must never read
# settings (tests set DATABASE_URL after import; doing it eagerly would cache the wrong URL).
@lru_cache
def get_engine() -> AsyncEngine:
    s = get_settings()
    return create_async_engine(
        s.database_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=s.stackd_db_pool_size,
        max_overflow=s.stackd_db_max_overflow,
        pool_timeout=s.stackd_db_pool_timeout_seconds,
        connect_args={"server_settings": _server_settings()},
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False, class_=AsyncSession)


def SessionLocal() -> AsyncSession:
    return get_sessionmaker()()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session
