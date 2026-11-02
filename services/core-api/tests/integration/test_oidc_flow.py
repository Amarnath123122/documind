"""Full Authorization Code + PKCE flow against the local mock IdP (Compose service `oidc`)."""

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from documind.common import config
from documind.identity import oidc

pytestmark = pytest.mark.db
IDP = "http://localhost:8080"


@pytest.fixture
def oidc_mode(monkeypatch):
    def use(issuer_id: str):
        monkeypatch.setenv("AUTH_MODE", "oidc")
        monkeypatch.setenv("OIDC_ISSUER", f"{IDP}/{issuer_id}")
        monkeypatch.setenv("OIDC_CLIENT_ID", "documind-web")
        monkeypatch.setenv("OIDC_CLIENT_SECRET", "local-test-secret")
        monkeypatch.setenv("OIDC_REDIRECT_URI", "http://test/v1/auth/callback")
        config.get_settings.cache_clear()
        oidc._cache.clear()
    yield use
    monkeypatch.undo()
    config.get_settings.cache_clear()
    oidc._cache.clear()


async def login_via_idp(issuer_id: str, oidc_mode) -> tuple[AsyncClient, httpx.Response]:
    oidc_mode(issuer_id)
    from documind.main import app
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    start = await c.get("/v1/auth/login")
    assert start.status_code == 302 and oidc.FLOW_COOKIE in c.cookies
    q = parse_qs(urlparse(start.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["response_type"] == ["code"]
    async with httpx.AsyncClient() as browser:  # the user's browser at the IdP
        idp = await browser.get(start.headers["location"])
    assert idp.status_code == 302
    back = parse_qs(urlparse(idp.headers["location"]).query)
    done = await c.get("/v1/auth/callback", params={"code": back["code"][0],
                                                    "state": back["state"][0]})
    return c, done


async def test_provisioned_user_signs_in(oidc_mode):
    c, done = await login_via_idp("documind", oidc_mode)
    assert done.status_code == 302 and done.headers["location"] == "/"
    me = await c.get("/v1/me")
    assert me.status_code == 200 and me.json()["email"] == "alice@alpha.test"
    assert (await c.post("/v1/dev/login", json={"email": "bob@beta.test"})).status_code == 404
    await c.aclose()


async def test_unprovisioned_identity_gets_no_session(oidc_mode):
    c, done = await login_via_idp("documind-unprovisioned", oidc_mode)
    assert done.status_code == 401 and done.json()["code"] == "NOT_PROVISIONED"
    assert "dm_session" not in c.cookies
    await c.aclose()


async def test_state_mismatch_and_missing_flow_cookie_rejected(oidc_mode):
    oidc_mode("documind")
    from documind.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.get("/v1/auth/login")
        r = await c.get("/v1/auth/callback", params={"code": "x", "state": "forged"})
        assert r.status_code == 401 and r.json()["code"] == "OIDC_FAILED"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/v1/auth/callback", params={"code": "x", "state": "y"})
        assert r.status_code == 401
