"""Async SQLAlchemy engine, session factory and declarative base."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, MetaData, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# 64-bit integers everywhere except SQLite, where only INTEGER PRIMARY KEY autoincrements.
BigInt = BigInteger().with_variant(Integer(), "sqlite")

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSON,
        list[str]: JSON,
        int: BigInt,
    }


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    """Stripe-style identifiers: ``prj_3f9c2a...`` (prefix + 20 hex chars)."""
    return f"{prefix}_{secrets.token_hex(10)}"


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; normalise everything to aware UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class Database:
    def __init__(self, url: str) -> None:
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs = {"connect_args": {"timeout": 30}}
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        from . import models  # noqa: F401 - register tables

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
