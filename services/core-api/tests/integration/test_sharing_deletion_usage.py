"""Phase 3: deletion lifecycle, restricted documents + grants (in RLS), usage quotas."""

import uuid

import pytest

from documind.common.storage import ORIGINALS, QUARANTINE, get_storage
from documind.dev_seed import ALPHA, BETA, CAROL, WS_ALPHA_HR
from documind.ingestion.worker import process_one
from helpers import TXT, admin_sql, upload

pytestmark = pytest.mark.db


async def drain():
    while await process_one():
        pass


async def ready_doc(client, body: bytes, name: str | None = None) -> str:
    t, done = await upload(client, WS_ALPHA_HR, name or f"d-{uuid.uuid4().hex[:6]}.txt", body, TXT)
    assert done.status_code == 202, done.text
    await drain()
    return t["document_id"]


async def search_ids(client, q: str) -> set[str]:
    r = await client.get(f"/v1/workspaces/{WS_ALPHA_HR}/search", params={"q": q})
    return {h["document_id"] for h in r.json()} if r.status_code == 200 else set()


@pytest.fixture(autouse=True)
async def _clean_queue():
    await drain()


# ---------- deletion ----------

async def test_delete_denies_immediately_then_cleans_everything(alice):
    code = uuid.uuid4().hex[:8]
    doc = await ready_doc(alice, f"Travel reimbursement code {code} applies.".encode())
    assert doc in await search_ids(alice, f"reimbursement {code}")

    r = await alice.delete(f"/v1/documents/{doc}")
    assert r.status_code == 202 and r.json()["status"] == "deleting"
    # Before the worker runs: already gone from search and direct reads still say deleting.
    assert doc not in await search_ids(alice, f"reimbursement {code}")

    await drain()
    assert (await alice.get(f"/v1/documents/{doc}")).status_code == 404
    left = await admin_sql(
        "SELECT (SELECT count(*) FROM document_chunks WHERE document_id = :d) AS c, "
        "(SELECT count(*) FROM document_pages WHERE document_id = :d) AS p", {"d": doc})
    assert (left[0].c, left[0].p) == (0, 0)
    for container in (QUARANTINE, ORIGINALS):
        assert await get_storage().info(container, f"{ALPHA}/{doc}/v1") is None
    audit = await admin_sql("SELECT action FROM audit_events WHERE target_id = :d", {"d": doc})
    assert "document.delete_requested" in {a.action for a in audit}


async def test_delete_is_idempotent_and_supports_if_match(alice):
    doc = await ready_doc(alice, b"idempotent delete")
    assert (await alice.delete(f"/v1/documents/{doc}",
                               headers={"If-Match": '"99"'})).status_code == 412
    assert (await alice.delete(f"/v1/documents/{doc}", headers={"If-Match": '"1"'})).status_code == 202
    assert (await alice.delete(f"/v1/documents/{doc}")).status_code == 202
    jobs = await admin_sql("SELECT count(*) AS n FROM jobs WHERE document_id = :d "
                           "AND kind = 'cleanup'", {"d": doc})
    assert jobs[0].n == 1


async def test_delete_during_ingest_never_resurrects(alice):
    t, _ = await upload(alice, WS_ALPHA_HR, "race.txt", b"racing the worker", TXT)
    assert (await alice.delete(f"/v1/documents/{t['document_id']}")).status_code == 202
    await drain()  # ingest job (v1) abandons, cleanup job (v2) finishes
    st = await admin_sql("SELECT status FROM documents WHERE id = :d", {"d": t["document_id"]})
    assert st[0].status == "deleted"
    kinds = {r.kind: r.status for r in await admin_sql(
        "SELECT kind, status FROM jobs WHERE document_id = :d", {"d": t["document_id"]})}
    assert kinds == {"ingest": "abandoned", "cleanup": "succeeded"}
    assert await get_storage().info(ORIGINALS, f"{ALPHA}/{t['document_id']}/v1") is None


async def test_viewer_cannot_delete_and_foreign_user_gets_404(carol, bob, alice):
    doc = await ready_doc(alice, b"protected")
    r = await carol.delete(f"/v1/documents/{doc}")
    assert r.status_code == 403 and r.json()["code"] == "DOCUMENT_MANAGE_DENIED"
    assert (await bob.delete(f"/v1/documents/{doc}")).status_code == 404


async def test_cleanup_failure_needs_attention_not_deleted(alice):
    from documind.common.storage import BlobStorage

    class Broken(BlobStorage):
        async def delete(self, *a, **kw):
            raise ConnectionError("storage down")

    doc = await ready_doc(alice, b"stuck cleanup")
    await alice.delete(f"/v1/documents/{doc}")
    await admin_sql("UPDATE jobs SET max_attempts = 1 WHERE document_id = :d AND kind = 'cleanup'",
                    {"d": doc})
    real = get_storage()
    assert await process_one(storage=Broken(real._conn, real._public_base))
    st = await admin_sql("SELECT status, failure_reason FROM documents WHERE id = :d", {"d": doc})
    assert (st[0].status, st[0].failure_reason) == ("needs_attention", "cleanup_failed")


# ---------- restricted documents + grants ----------

async def test_restricted_document_hidden_everywhere_until_granted(alice, carol):
    code = uuid.uuid4().hex[:8]
    doc = await ready_doc(alice, f"Salary band {code} is confidential.".encode())
    assert (await alice.patch(f"/v1/documents/{doc}",
                              json={"visibility": "restricted"})).status_code == 200

    # Carol (same tenant, viewer): not in list, get, search, or chat context.
    assert (await carol.get(f"/v1/documents/{doc}")).status_code == 404
    listed = (await carol.get(f"/v1/workspaces/{WS_ALPHA_HR}/documents?limit=100")).json()
    assert doc not in {d["id"] for d in listed["items"]}
    assert doc not in await search_ids(carol, f"salary band {code}")

    # Grant -> visible; revoke -> gone on the very next request.
    assert (await alice.post(f"/v1/documents/{doc}/grants",
                             json={"email": "carol@alpha.test"})).status_code == 204
    assert (await carol.get(f"/v1/documents/{doc}")).status_code == 200
    assert doc in await search_ids(carol, f"salary band {code}")
    assert (await alice.delete(f"/v1/documents/{doc}/grants/{CAROL}")).status_code == 204
    assert (await carol.get(f"/v1/documents/{doc}")).status_code == 404
    assert doc not in await search_ids(carol, f"salary band {code}")


async def test_revoked_grant_makes_old_citation_unavailable(alice, carol):
    code = uuid.uuid4().hex[:8]
    doc = await ready_doc(alice, f"Bonus pool {code} equals 7 percent.".encode())
    await alice.patch(f"/v1/documents/{doc}", json={"visibility": "restricted"})
    await alice.post(f"/v1/documents/{doc}/grants", json={"email": "carol@alpha.test"})
    r = await carol.post("/v1/chat/turns", json={"workspace_id": str(WS_ALPHA_HR),
                                                 "question": f"bonus pool {code}"})
    turn = r.json()
    body = (await carol.get(turn["stream_url"])).text
    assert doc in body
    await alice.delete(f"/v1/documents/{doc}/grants/{CAROL}")
    conv = (await carol.get(f"/v1/chat/conversations/{turn['conversation_id']}")).json()
    cites = [c for c in conv["turns"][0]["citations"] if c["document_id"] == doc]
    assert cites and cites[0]["available"] is False and cites[0]["quote_preview"] is None


async def test_cannot_share_with_other_tenant_or_as_viewer(alice, carol):
    doc = await ready_doc(alice, b"share rules")
    r = await alice.post(f"/v1/documents/{doc}/grants", json={"email": "bob@beta.test"})
    assert r.status_code == 404 and r.json()["code"] == "MEMBER_NOT_FOUND"
    r = await carol.patch(f"/v1/documents/{doc}", json={"visibility": "restricted"})
    assert r.status_code == 403


async def test_rls_blocks_raw_sql_read_of_restricted_doc():
    from sqlalchemy import text

    from documind.common.db import unit_of_work
    doc = uuid.uuid4()
    await admin_sql(
        "INSERT INTO documents (id, tenant_id, workspace_id, filename, status, created_by, "
        "visibility) VALUES (:d, :t, :w, 'secret.txt', 'ready', "
        "'00000000-0000-4000-8000-00000000a11c', 'restricted')",
        {"d": doc, "t": ALPHA, "w": WS_ALPHA_HR})
    async with unit_of_work(CAROL) as s:
        n = (await s.execute(text("SELECT count(*) FROM documents WHERE id = :d"),
                             {"d": doc})).scalar_one()
    assert n == 0


# ---------- usage + quotas ----------

async def test_usage_is_recorded_and_visible_to_admin_only(alice, carol):
    before = (await alice.get(f"/v1/tenants/{ALPHA}/usage")).json()
    await ready_doc(alice, b"x" * 1000)
    r = await alice.post("/v1/chat/turns", json={"workspace_id": str(WS_ALPHA_HR),
                                                 "question": "casual leave"})
    await alice.get(r.json()["stream_url"])
    after = (await alice.get(f"/v1/tenants/{ALPHA}/usage")).json()
    assert after["storage_bytes"]["used"] >= before["storage_bytes"]["used"] + 1000
    assert after["chat_tokens"]["used"] > before["chat_tokens"]["used"]
    assert (await carol.get(f"/v1/tenants/{ALPHA}/usage")).status_code == 404
    assert (await alice.get(f"/v1/tenants/{BETA}/usage")).status_code == 404


async def test_deleting_returns_storage_quota(alice):
    doc = await ready_doc(alice, b"y" * 5000)
    mid = (await alice.get(f"/v1/tenants/{ALPHA}/usage")).json()["storage_bytes"]["used"]
    await alice.delete(f"/v1/documents/{doc}")
    await drain()
    end = (await alice.get(f"/v1/tenants/{ALPHA}/usage")).json()["storage_bytes"]["used"]
    assert end == mid - 5000


async def test_quota_exhaustion_returns_429(alice):
    await admin_sql("UPDATE tenant_quotas SET monthly_chat_tokens = 0, storage_bytes = 0 "
                    "WHERE tenant_id = :t", {"t": ALPHA})
    try:
        r = await alice.post("/v1/chat/turns", json={"workspace_id": str(WS_ALPHA_HR),
                                                     "question": "anything"})
        assert r.status_code == 429 and r.json()["code"] == "QUOTA_EXCEEDED"
        r = await alice.post(f"/v1/workspaces/{WS_ALPHA_HR}/uploads",
                             json={"filename": "q.txt", "size_bytes": 10, "mime": TXT})
        assert r.status_code == 429
    finally:
        await admin_sql("UPDATE tenant_quotas SET monthly_chat_tokens = 2000000, "
                        "storage_bytes = 5368709120 WHERE tenant_id = :t", {"t": ALPHA})
