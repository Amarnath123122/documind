"""Adversarial two-tenant tests. Require Compose Postgres, migrated + seeded."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from documind.common.db import unit_of_work
from documind.dev_seed import ALICE, ALPHA, BETA, BOB, DOCS, WS_ALPHA_HR, WS_BETA_LEGAL

pytestmark = pytest.mark.db
ALPHA_DOC, BETA_DOC = DOCS[0][0], DOCS[1][0]


def _body(filename: str) -> dict:
    return {"filename": filename, "size_bytes": 100, "mime": "application/pdf"}


# ---------- API layer ----------

async def test_unauthenticated_is_401():
    from documind.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/workspaces")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_forged_cookie_is_401():
    from documind.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t",
                           cookies={"dm_session": "forged.value"}) as c:
        assert (await c.get("/v1/me")).status_code == 401


async def test_alice_sees_only_alpha(alice):
    ws = (await alice.get("/v1/workspaces")).json()
    assert str(WS_ALPHA_HR) in {w["id"] for w in ws}
    assert {w["tenant_id"] for w in ws} == {str(ALPHA)}


async def test_bob_cannot_read_alpha_document_by_guessing_id(bob):
    r = await bob.get(f"/v1/documents/{ALPHA_DOC}")
    assert r.status_code == 404
    assert "employment-policy" not in r.text


async def test_cross_tenant_and_nonexistent_are_indistinguishable(bob):
    a = await bob.get(f"/v1/documents/{ALPHA_DOC}")
    b = await bob.get(f"/v1/documents/{uuid.uuid4()}")
    assert a.status_code == b.status_code == 404
    assert a.json()["code"] == b.json()["code"]


async def test_bob_cannot_list_or_write_into_alpha_workspace(bob):
    assert (await bob.get(f"/v1/workspaces/{WS_ALPHA_HR}/documents")).status_code == 404
    r = await bob.post(f"/v1/workspaces/{WS_ALPHA_HR}/uploads", json=_body("x.pdf"))
    assert r.status_code == 404


async def test_viewer_cannot_create_but_can_read(carol):
    r = await carol.post(f"/v1/workspaces/{WS_ALPHA_HR}/uploads", json=_body("c.pdf"))
    assert r.status_code == 403 and r.json()["code"] == "DOCUMENT_CREATE_DENIED"
    assert (await carol.get(f"/v1/documents/{ALPHA_DOC}")).status_code == 200


async def test_csrf_required_for_mutations(alice):
    del alice.headers["X-CSRF-Token"]
    r = await alice.post(f"/v1/workspaces/{WS_ALPHA_HR}/uploads", json=_body("a.pdf"))
    assert r.status_code == 403 and r.json()["code"] == "CSRF_FAILED"


async def test_create_and_keyset_pagination(alice):
    tag = uuid.uuid4().hex[:8]
    for i in range(3):
        r = await alice.post(f"/v1/workspaces/{WS_ALPHA_HR}/uploads",
                             json=_body(f"p-{tag}-{i}.pdf"))
        assert r.status_code == 201
    seen, cursor = [], None
    while True:
        q = f"?limit=2&cursor={cursor}" if cursor else "?limit=2"
        page = (await alice.get(f"/v1/workspaces/{WS_ALPHA_HR}/documents{q}")).json()
        seen += [d["id"] for d in page["items"]]
        if not (cursor := page["next_cursor"]):
            break
    assert len(seen) == len(set(seen)), "keyset pagination returned duplicates"
    assert sum(1 for _ in seen) >= 4


async def test_path_traversal_filename_rejected(alice):
    r = await alice.post(f"/v1/workspaces/{WS_ALPHA_HR}/uploads",
                         json=_body("../../etc/passwd"))
    assert r.status_code == 422


# ---------- Database layer (RLS, as the real non-owner app role) ----------

async def test_app_role_is_not_privileged():
    async with unit_of_work(None) as s:
        r = (await s.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))).one()
    assert r == (False, False)


async def test_rls_hides_beta_rows_even_with_raw_sql():
    async with unit_of_work(ALICE) as s:
        tenants = {r[0] for r in await s.execute(text("SELECT DISTINCT tenant_id FROM documents"))}
    assert tenants == {ALPHA}


async def test_rls_anonymous_sees_nothing():
    async with unit_of_work(None) as s:
        n = (await s.execute(text("SELECT count(*) FROM documents"))).scalar_one()
    assert n == 0


async def test_rls_with_check_blocks_cross_tenant_insert():
    with pytest.raises(Exception, match="row-level security"):
        async with unit_of_work(ALICE) as s:
            await s.execute(text(
                "INSERT INTO documents (id, tenant_id, workspace_id, filename, status, "
                "created_by) VALUES (:i, :t, :w, 'evil.pdf', 'ready', :by)"),
                {"i": uuid.uuid4(), "t": BETA, "w": WS_BETA_LEGAL, "by": ALICE})


async def test_rls_blocks_cross_tenant_update():
    async with unit_of_work(ALICE) as s:
        res = await s.execute(text("UPDATE documents SET filename='pwned' WHERE id=:i"),
                              {"i": BETA_DOC})
    assert res.rowcount == 0


async def test_audit_is_append_only_for_app_role():
    with pytest.raises(Exception, match="permission denied"):
        async with unit_of_work(ALICE) as s:
            await s.execute(text("DELETE FROM audit_events"))


async def test_pooled_connection_does_not_leak_identity():
    # Many alternating transactions over a small pool: context is transaction-local.
    for _ in range(10):
        async with unit_of_work(BOB) as s:
            t = {r[0] for r in await s.execute(text("SELECT tenant_id FROM documents"))}
            assert t == {BETA}
        async with unit_of_work(None) as s:
            assert (await s.execute(text("SELECT count(*) FROM documents"))).scalar_one() == 0


async def test_unsafe_request_id_is_not_echoed():
    from documind.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        ok = await c.get("/healthz", headers={"X-Request-ID": "abc-123-def-456"})
        evil = await c.get("/healthz", headers={"X-Request-ID": "x\r\nlevel=CRITICAL forged"})
    assert ok.headers["X-Request-ID"] == "abc-123-def-456"
    assert "forged" not in evil.headers["X-Request-ID"]
