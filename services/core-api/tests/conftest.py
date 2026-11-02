"""Tests run against their own database, `documind_test`, never the dev app's `documind`.

At session start it is created if missing (same roles, schema and extension as the dev
DB), migrated to head and seeded. Test data therefore never appears in the running app.
"""

import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient

TEST_DB = "documind_test"
_HOST = os.environ.get("TEST_DB_HOST", "localhost:55432")
_PW = {
    "postgres": os.environ.get("POSTGRES_PASSWORD", "dev-superuser-pw"),
    "documind_owner": os.environ.get("DOCUMIND_OWNER_PASSWORD", "dev-owner-pw"),
    "documind_app": os.environ.get("DOCUMIND_APP_PASSWORD", "dev-app-pw"),
    "documind_worker": os.environ.get("DOCUMIND_WORKER_PASSWORD", "dev-worker-pw"),
}


def _url(role: str, db: str = TEST_DB) -> str:
    return f"postgresql+asyncpg://{role}:{_PW[role]}@{_HOST}/{db}"


# Must happen before anything imports documind settings (they are cached).
os.environ.update({
    "DATABASE_URL": _url("documind_app"),
    "WORKER_DATABASE_URL": _url("documind_worker"),
    "MIGRATION_DATABASE_URL": _url("documind_owner"),
    "SEED_DATABASE_URL": _url("postgres"),
})


async def _ensure_test_db() -> None:
    import asyncpg

    sys_conn = await asyncpg.connect(_url("postgres", "postgres").replace("+asyncpg", ""))
    try:
        if not await sys_conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB):
            await sys_conn.execute(f"CREATE DATABASE {TEST_DB}")
    finally:
        await sys_conn.close()
    conn = await asyncpg.connect(_url("postgres").replace("+asyncpg", ""))
    try:
        # Mirrors infra/compose/initdb/01-roles.sh (roles are cluster-wide and already exist).
        await conn.execute("""
            CREATE SCHEMA IF NOT EXISTS documind AUTHORIZATION documind_owner;
            REVOKE ALL ON SCHEMA public FROM PUBLIC;
            GRANT USAGE ON SCHEMA documind TO documind_app, documind_worker;
            CREATE EXTENSION IF NOT EXISTS vector SCHEMA documind;
        """)
    finally:
        await conn.close()


def pytest_sessionstart(session):
    if os.environ.get("SKIP_TEST_DB"):
        return
    try:
        asyncio.run(_ensure_test_db())
    except OSError as e:  # DB not running: unit tests still work; db tests will fail loudly
        print(f"\n[conftest] test database unavailable ({e}); DB tests will fail")
        return
    from alembic import command
    from alembic.config import Config

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "alembic"))
    command.upgrade(cfg, "head")
    from documind.dev_seed import seed
    asyncio.run(seed())


async def login(email: str) -> AsyncClient:
    from documind.main import app

    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    r = await c.post("/v1/dev/login", json={"email": email})
    assert r.status_code == 204, r.text
    c.headers["X-CSRF-Token"] = c.cookies["dm_csrf"]
    return c


@pytest.fixture
async def alice():
    c = await login("alice@alpha.test")
    yield c
    await c.aclose()


@pytest.fixture
async def carol():
    c = await login("carol@alpha.test")
    yield c
    await c.aclose()


@pytest.fixture
async def bob():
    c = await login("bob@beta.test")
    yield c
    await c.aclose()
