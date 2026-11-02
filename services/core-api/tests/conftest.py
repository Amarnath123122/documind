import pytest
from httpx import ASGITransport, AsyncClient


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
