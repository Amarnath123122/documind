"""Upload -> quarantine -> worker pipeline. Requires Compose db + azurite, worker container stopped
(scripts/dev.ps1 test does this) so these tests drive the worker deterministically."""

import httpx
import pytest
from sqlalchemy import text

from documind.common.db import worker_unit_of_work
from documind.common.storage import QUARANTINE, BlobStorage, get_storage
from documind.dev_seed import ALPHA, WS_ALPHA_HR
from documind.ingestion.worker import claim, process_one
from helpers import EICAR, PDF, TXT, admin_sql, make_pdf, put_blob, start_upload, upload

pytestmark = pytest.mark.db


async def drain(**kw) -> None:
    while await process_one(**kw):
        pass


async def doc(client, doc_id) -> dict:
    r = await client.get(f"/v1/documents/{doc_id}")
    assert r.status_code == 200, r.text
    return r.json()


async def job_of(doc_id) -> dict:
    rows = await admin_sql("SELECT status, attempts, last_error FROM jobs WHERE document_id = :d",
                           {"d": doc_id})
    assert len(rows) == 1
    return rows[0]._asdict()


async def make_claimable(doc_id) -> None:
    await admin_sql("UPDATE jobs SET run_after = now() WHERE document_id = :d", {"d": doc_id})


class FlakyStorage(BlobStorage):
    """Simulates a transient storage outage for the first `failures` reads."""

    def __init__(self, failures: int):
        real = get_storage()
        super().__init__(real._conn, real._public_base)
        self.failures = failures

    async def read_all(self, *a, **kw):
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("simulated outage")
        return await super().read_all(*a, **kw)


@pytest.fixture(autouse=True)
async def _empty_queue():
    await drain()


# ---------- happy paths ----------

async def test_pdf_upload_reaches_ready_with_page_text(alice):
    pdf = make_pdf(["Casual leave: 12 days per calendar year", "Sick leave: 10 days"])
    ticket, done = await upload(alice, WS_ALPHA_HR, "policy.pdf", pdf, PDF)
    assert done.status_code == 202 and done.json()["status"] == "quarantined"
    await drain()
    d = await doc(alice, ticket["document_id"])
    assert d["status"] == "ready" and d["page_count"] == 2
    pages = await admin_sql("SELECT page_no, text FROM document_pages WHERE document_id = :d "
                            "ORDER BY page_no", {"d": ticket["document_id"]})
    assert "Casual leave" in pages[0].text and "Sick leave" in pages[1].text
    # Quarantine copy is gone once scanned; sha256 recorded.
    assert await get_storage().info(QUARANTINE, f"{ALPHA}/{ticket['document_id']}/v1") is None
    sha = await admin_sql("SELECT sha256 FROM documents WHERE id = :d", {"d": ticket["document_id"]})
    assert len(sha[0].sha256) == 64


async def test_text_pages_split_on_form_feed(alice):
    ticket, _ = await upload(alice, WS_ALPHA_HR, "notes.txt", b"one\fthree\ftwo", TXT)
    await drain()
    assert (await doc(alice, ticket["document_id"]))["page_count"] == 3


async def test_complete_twice_is_idempotent_and_enqueues_once(alice):
    ticket, first = await upload(alice, WS_ALPHA_HR, "a.txt", b"hello", TXT)
    second = await alice.post(f"/v1/uploads/{ticket['upload_id']}/complete")
    assert first.status_code == second.status_code == 202
    assert (await job_of(ticket["document_id"]))["status"] == "queued"


async def test_idempotency_key_returns_same_document(alice):
    a = await start_upload(alice, WS_ALPHA_HR, "k.txt", b"x", TXT, key="key-123-abc")
    b = await start_upload(alice, WS_ALPHA_HR, "k.txt", b"x", TXT, key="key-123-abc")
    assert a["document_id"] == b["document_id"] and a["upload_id"] == b["upload_id"]


# ---------- verification at completion ----------

async def test_size_mismatch_rejected_and_bytes_discarded(alice):
    ticket, done = await upload(alice, WS_ALPHA_HR, "s.txt", b"short", TXT, declared_size=999)
    assert done.status_code == 422 and done.json()["code"] == "SIZE_MISMATCH"
    d = await doc(alice, ticket["document_id"])
    assert d["status"] == "failed" and d["failure_reason"] == "size_mismatch"
    assert await get_storage().info(QUARANTINE, f"{ALPHA}/{ticket['document_id']}/v1") is None


async def test_spoofed_content_type_rejected(alice):
    _, done = await upload(alice, WS_ALPHA_HR, "fake.pdf", b"just text, not a pdf", PDF)
    assert done.status_code == 422 and done.json()["code"] == "CONTENT_TYPE_MISMATCH"


async def test_unsupported_type_and_oversize_rejected_before_upload(alice):
    r = await alice.post(f"/v1/workspaces/{WS_ALPHA_HR}/uploads",
                         json={"filename": "x.exe", "size_bytes": 10,
                               "mime": "application/x-msdownload"})
    assert r.status_code == 422 and r.json()["code"] == "UNSUPPORTED_TYPE"
    r = await alice.post(f"/v1/workspaces/{WS_ALPHA_HR}/uploads",
                         json={"filename": "big.pdf", "size_bytes": 10**10, "mime": PDF})
    assert r.status_code == 413


async def test_complete_before_bytes_uploaded_is_409(alice):
    t = await start_upload(alice, WS_ALPHA_HR, "none.txt", b"abc", TXT)
    r = await alice.post(f"/v1/uploads/{t['upload_id']}/complete")
    assert r.status_code == 409 and r.json()["code"] == "UPLOAD_MISSING"


# ---------- capability + authorization ----------

async def test_upload_sas_is_write_only(alice):
    t = await start_upload(alice, WS_ALPHA_HR, "w.txt", b"data", TXT)
    assert (await put_blob(t, b"data")).status_code == 201
    async with httpx.AsyncClient() as raw:
        assert (await raw.get(t["upload_url"])).status_code == 403


async def test_only_the_uploader_can_complete(alice, carol, bob):
    t = await start_upload(alice, WS_ALPHA_HR, "mine.txt", b"abc", TXT)
    await put_blob(t, b"abc")
    for other in (carol, bob):  # same tenant, other tenant
        r = await other.post(f"/v1/uploads/{t['upload_id']}/complete")
        assert r.status_code == 404


async def test_viewer_and_foreign_user_cannot_start_upload(carol, bob):
    body = {"filename": "c.txt", "size_bytes": 3, "mime": TXT}
    r = await carol.post(f"/v1/workspaces/{WS_ALPHA_HR}/uploads", json=body)
    assert r.status_code == 403 and r.json()["code"] == "DOCUMENT_CREATE_DENIED"
    assert (await bob.post(f"/v1/workspaces/{WS_ALPHA_HR}/uploads", json=body)).status_code == 404


# ---------- worker: permanent failures ----------

async def test_malware_fails_permanently_without_retry(alice):
    ticket, _ = await upload(alice, WS_ALPHA_HR, "eicar.txt", EICAR, TXT)
    await drain()
    d = await doc(alice, ticket["document_id"])
    assert d["status"] == "failed" and d["failure_reason"] == "malware_detected"
    job = await job_of(ticket["document_id"])
    assert job["status"] == "failed" and job["attempts"] == 1
    assert await get_storage().info(QUARANTINE, f"{ALPHA}/{ticket['document_id']}/v1") is None


async def test_corrupt_pdf_fails_permanently(alice):
    ticket, _ = await upload(alice, WS_ALPHA_HR, "bad.pdf", b"%PDF-1.4 garbage", PDF)
    await drain()
    assert (await doc(alice, ticket["document_id"]))["failure_reason"] == "unparseable_pdf"


async def test_blob_overwritten_after_verification_is_rejected(alice):
    ticket, _ = await upload(alice, WS_ALPHA_HR, "swap.txt", b"benign", TXT)
    await put_blob(ticket, b"evil!!")  # SAS still valid: attacker swaps bytes post-check
    await drain()
    d = await doc(alice, ticket["document_id"])
    assert d["failure_reason"] == "content_changed_after_verification"


# ---------- worker: retries, dead letter, races ----------

async def test_transient_failure_retries_then_succeeds(alice):
    ticket, _ = await upload(alice, WS_ALPHA_HR, "flaky.txt", b"retry me", TXT)
    assert await process_one(storage=FlakyStorage(failures=1))
    job = await job_of(ticket["document_id"])
    assert job["status"] == "queued" and job["attempts"] == 1
    assert job["last_error"] == "ConnectionError"  # class name only, no content
    assert (await doc(alice, ticket["document_id"]))["status"] == "scanning"
    await make_claimable(ticket["document_id"])
    await drain()
    assert (await doc(alice, ticket["document_id"]))["status"] == "ready"


async def test_dead_letter_after_max_attempts(alice):
    ticket, _ = await upload(alice, WS_ALPHA_HR, "dead.txt", b"never", TXT)
    await admin_sql("UPDATE jobs SET max_attempts = 2 WHERE document_id = :d",
                    {"d": ticket["document_id"]})
    flaky = FlakyStorage(failures=99)
    for _ in range(2):
        await make_claimable(ticket["document_id"])
        assert await process_one(storage=flaky)
    job = await job_of(ticket["document_id"])
    assert job["status"] == "dead" and job["attempts"] == 2
    d = await doc(alice, ticket["document_id"])
    assert d["status"] == "failed" and d["failure_reason"] == "processing_failed_after_retries"


async def test_deleting_document_is_never_revived(alice):
    ticket, _ = await upload(alice, WS_ALPHA_HR, "del.txt", b"bye", TXT)
    await admin_sql("UPDATE documents SET status = 'deleting' WHERE id = :d",
                    {"d": ticket["document_id"]})
    await drain()
    assert (await job_of(ticket["document_id"]))["status"] == "abandoned"
    assert (await doc(alice, ticket["document_id"]))["status"] == "deleting"


async def test_expired_lease_is_reclaimed(alice):
    ticket, _ = await upload(alice, WS_ALPHA_HR, "crash.txt", b"crash", TXT)
    crashed = await claim()  # a worker claims, then "dies" without finishing
    assert crashed and str(crashed.document_id) == ticket["document_id"]
    assert not await process_one()  # lease still held: nobody else may take it
    await admin_sql("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = :i",
                    {"i": crashed.id})
    await drain()
    assert (await doc(alice, ticket["document_id"]))["status"] == "ready"
    assert (await job_of(ticket["document_id"]))["attempts"] == 2


# ---------- worker DB role ----------

async def test_worker_role_sees_only_the_job_tenant():
    async with worker_unit_of_work(ALPHA) as s:
        tenants = {r[0] for r in await s.execute(text("SELECT DISTINCT tenant_id FROM documents"))}
    assert tenants == {ALPHA}
    async with worker_unit_of_work(None) as s:
        assert (await s.execute(text("SELECT count(*) FROM documents"))).scalar_one() == 0


async def test_worker_role_cannot_touch_identity_or_audit():
    for sql in ("SELECT * FROM memberships",
                ("INSERT INTO audit_events (tenant_id, actor_user_id, action) "
                 "VALUES (gen_random_uuid(), gen_random_uuid(), 'x')")):
        with pytest.raises(Exception, match="permission denied"):
            async with worker_unit_of_work(ALPHA) as s:
                await s.execute(text(sql))
