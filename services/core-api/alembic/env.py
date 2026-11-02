"""Migrations run as documind_owner (never as the runtime app role)."""

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from documind.common.config import get_settings


def _run(connection) -> None:
    context.configure(connection=connection, version_table_schema="documind")
    with context.begin_transaction():
        context.run_migrations()


async def _main() -> None:
    engine = create_async_engine(get_settings().migration_database_url)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


asyncio.run(_main())
