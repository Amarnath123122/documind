"""Database access. Every unit of work runs as a non-owner role with RLS context.

Context is set transaction-locally (set_config(..., true)), so a pooled connection
never leaks one request's (or job's) identity into the next.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from documind.common.config import get_settings

_makers: dict[str, async_sessionmaker[AsyncSession]] = {}


def _maker(url: str) -> async_sessionmaker[AsyncSession]:
    if url not in _makers:
        _makers[url] = async_sessionmaker(
            create_async_engine(url, pool_pre_ping=True), expire_on_commit=False)
    return _makers[url]


def get_engine() -> AsyncEngine:
    return _maker(get_settings().database_url).kw["bind"]


@asynccontextmanager
async def unit_of_work(user_id: UUID | None) -> AsyncIterator[AsyncSession]:
    """API transaction as documind_app, scoped to a verified principal (or anonymous)."""
    async with _maker(get_settings().database_url)() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"),
            {"uid": str(user_id) if user_id else ""},
        )
        yield session


@asynccontextmanager
async def worker_unit_of_work(tenant_id: UUID | None) -> AsyncIterator[AsyncSession]:
    """Worker transaction as documind_worker. tenant_id must come from a claimed job row."""
    async with _maker(get_settings().worker_database_url)() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": str(tenant_id) if tenant_id else ""},
        )
        yield session
