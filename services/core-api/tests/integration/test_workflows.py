"""Compare-documents workflow: draft -> four-eyes approval -> export, with live-source checks."""

import uuid

import pytest

from documind.dev_seed import WS_ALPHA_HR
from documind.ingestion.worker import process_one
from helpers import TXT, admin_sql, upload

pytestmark = pytest.mark.db


async def drain():
    while await process_one():
        pass


@pytest.fixture
async def dave():
    from conftest import login
    c = await login("dave@alpha.test")
    yield c
    await c.aclose()


async def ready(client, body: str) -> str:
    t, done = await upload(client, WS_ALPHA_HR, f"wf-{uuid.uuid4().hex[:6]}.txt",
                           body.encode(), TXT)
    assert done.status_code == 202
    await drain()
    return t["document_id"]


async def draft(client, docs: list[str], title="Leave comparison") -> dict:
    r = await client.post("/v1/workflows", json={"workspace_id": str(WS_ALPHA_HR),
                                                 "title": title, "document_ids": docs})
    assert r.status_code == 201, r.text
    return r.json()


async def test_draft_is_cited_and_pending(dave):
    a = await ready(dave, "Casual leave is 12 days per year. Contact hr@alpha.test for leave.")
    b = await ready(dave, "Sick leave is 10 days per year with a medical certificate.")
    run = await draft(dave, [a, b])
    assert run["status"] == "pending_approval"
    assert "12 days" in run["draft"] and "10 days" in run["draft"] and "(p. 1)" in run["draft"]
    assert "hr@alpha.test" not in run["draft"] and "[EMAIL_REDACTED]" in run["draft"]
    assert [s["document_id"] for s in run["sources"]] == [a, b]


async def test_four_eyes_approval_then_export(dave, alice):
    run = await draft(dave, [await ready(dave, "Remote work is allowed two days per week.")])
    assert (await dave.get(f"/v1/workflows/{run['id']}/export")).status_code == 409
    r = await dave.post(f"/v1/workflows/{run['id']}/approve", json={})
    assert r.status_code == 403 and r.json()["code"] == "WORKFLOW_APPROVE_DENIED"

    r = await alice.post(f"/v1/workflows/{run['id']}/approve", json={"note": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    exp = await dave.get(f"/v1/workflows/{run['id']}/export")
    assert exp.status_code == 200 and "two days per week" in exp.text
    assert exp.headers["content-disposition"].startswith("attachment;")
    again = await alice.post(f"/v1/workflows/{run['id']}/approve", json={})
    assert again.status_code == 409
    actions = {r.action for r in await admin_sql(
        "SELECT action FROM audit_events WHERE target_id = :i", {"i": run["id"]})}
    assert {"workflow.created", "workflow.approved", "workflow.exported"} <= actions


async def test_admin_cannot_approve_own_report(alice):
    run = await draft(alice, [await ready(alice, "Holiday list is published every December.")])
    r = await alice.post(f"/v1/workflows/{run['id']}/approve", json={})
    assert r.status_code == 403 and r.json()["code"] == "FOUR_EYES"


async def test_schema_enforces_four_eyes_even_if_code_is_bypassed(dave):
    run = await draft(dave, [await ready(dave, "Canteen opens at 8 am.")])
    with pytest.raises(Exception, match="check constraint"):
        await admin_sql("UPDATE workflow_runs SET decided_by = created_by WHERE id = :i",
                        {"i": run["id"]})


async def test_rejection_blocks_export(dave, alice):
    run = await draft(dave, [await ready(dave, "Gym membership is reimbursed quarterly.")])
    r = await alice.post(f"/v1/workflows/{run['id']}/reject", json={"note": "Needs data"})
    assert r.json()["status"] == "rejected" and r.json()["decision_note"] == "Needs data"
    assert (await dave.get(f"/v1/workflows/{run['id']}/export")).json()["code"] == "NOT_APPROVED"


async def test_source_deleted_before_approval_blocks_it(dave, alice):
    doc = await ready(dave, "Shift allowance is 300 rupees per night.")
    run = await draft(dave, [doc])
    assert (await dave.delete(f"/v1/documents/{doc}")).status_code == 202
    r = await alice.post(f"/v1/workflows/{run['id']}/approve", json={})
    assert r.status_code == 409 and r.json()["code"] == "SOURCES_CHANGED"


async def test_source_deleted_after_approval_blocks_export(dave, alice):
    doc = await ready(dave, "Night shift cab service runs until 2 am.")
    run = await draft(dave, [doc])
    await alice.post(f"/v1/workflows/{run['id']}/approve", json={})
    await dave.delete(f"/v1/documents/{doc}")
    r = await dave.get(f"/v1/workflows/{run['id']}/export")
    assert r.status_code == 409 and r.json()["code"] == "SOURCES_CHANGED"


async def test_cannot_draft_from_invisible_or_foreign_documents(dave, alice, bob, carol):
    secret = await ready(alice, "Executive bonus pool details.")
    await alice.patch(f"/v1/documents/{secret}", json={"visibility": "restricted"})
    r = await dave.post("/v1/workflows", json={"workspace_id": str(WS_ALPHA_HR),
                                               "title": "x", "document_ids": [secret]})
    assert r.status_code == 404 and r.json()["code"] == "DOCUMENT_NOT_FOUND"
    body = {"workspace_id": str(WS_ALPHA_HR), "title": "x", "document_ids": [secret]}
    assert (await bob.post("/v1/workflows", json=body)).status_code == 404
    r = await carol.post("/v1/workflows", json=body)
    assert r.status_code == 403 and r.json()["code"] == "WORKFLOW_RUN_DENIED"


async def test_runs_visible_to_author_and_admins_only(dave, alice, carol, bob):
    run = await draft(dave, [await ready(dave, "Badge replacement costs 200 rupees.")])
    assert (await alice.get(f"/v1/workflows/{run['id']}")).status_code == 200
    for other in (carol, bob):
        assert (await other.get(f"/v1/workflows/{run['id']}")).status_code == 404
