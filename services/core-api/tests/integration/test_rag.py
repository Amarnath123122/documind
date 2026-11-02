"""Redaction -> index -> permission-filtered retrieval -> chat with validated citations."""

import json
import uuid

import pytest

from documind.chat import providers
from documind.dev_seed import ALPHA, WS_ALPHA_HR, WS_BETA_LEGAL
from documind.ingestion.worker import process_one
from helpers import PDF, TXT, admin_sql, make_pdf, upload

pytestmark = pytest.mark.db

ALPHA_POLICY = make_pdf([
    "Casual leave: 12 days per calendar year. Contact hr.alpha@alpha.test or 9876543210.",
    "Sick leave: 10 days per year with a medical certificate.",
])
BETA_POLICY = b"Beta confidential: casual leave is 99 days for executives."


async def drain():
    while await process_one():
        pass


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((fields["event"], json.loads(fields["data"])))
    return events


async def ask(client, ws, question: str, conversation_id=None) -> tuple[dict, list]:
    r = await client.post("/v1/chat/turns", json={
        "workspace_id": str(ws), "question": question, "conversation_id": conversation_id})
    assert r.status_code == 201, r.text
    turn = r.json()
    events = await client.get(turn["stream_url"])
    assert events.status_code == 200, events.text
    assert events.headers["content-type"].startswith("text/event-stream")
    return turn, parse_sse(events.text)


def final(events):
    return next(d for e, d in events if e == "completed")


@pytest.fixture(scope="module")
async def corpus():
    """Alpha + Beta each upload a leave policy; worker indexes both."""
    from conftest import login

    alice, bob = await login("alice@alpha.test"), await login("bob@beta.test")
    tag = uuid.uuid4().hex[:6]
    a, _ = await upload(alice, WS_ALPHA_HR, f"policy-{tag}.pdf", ALPHA_POLICY, PDF)
    b, _ = await upload(bob, WS_BETA_LEGAL, f"beta-{tag}.txt", BETA_POLICY, TXT)
    await drain()
    await alice.aclose()
    await bob.aclose()
    return {"alpha_doc": a["document_id"], "beta_doc": b["document_id"],
            "alpha_name": f"policy-{tag}.pdf"}


# ---------- indexing + privacy ----------

async def test_document_indexed_with_redacted_chunks_only(alice, corpus):
    d = (await alice.get(f"/v1/documents/{corpus['alpha_doc']}")).json()
    assert d["status"] == "ready"
    chunks = await admin_sql("SELECT text, embedding_model FROM document_chunks "
                             "WHERE document_id = :d", {"d": corpus["alpha_doc"]})
    blob = " ".join(c.text for c in chunks)
    assert "[EMAIL_REDACTED]" in blob and "[PHONE_REDACTED]" in blob
    assert "hr.alpha@alpha.test" not in blob and "9876543210" not in blob
    assert all(c.embedding_model == "hash-v1-256" for c in chunks)
    counts = await admin_sql("SELECT redaction_counts FROM documents WHERE id = :d",
                             {"d": corpus["alpha_doc"]})
    assert counts[0].redaction_counts == {"EMAIL": 1, "PHONE": 1}


async def test_app_role_cannot_read_raw_pages():
    from sqlalchemy import text

    from documind.common.db import unit_of_work
    from documind.dev_seed import ALICE
    with pytest.raises(Exception, match="permission denied"):
        async with unit_of_work(ALICE) as s:
            await s.execute(text("SELECT text FROM document_pages"))


# ---------- retrieval ----------

async def test_search_finds_right_page(alice, corpus):
    hits = (await alice.get(f"/v1/workspaces/{WS_ALPHA_HR}/search",
                            params={"q": "casual leave"})).json()
    # The dev DB accumulates copies of this policy across runs: any Alpha copy may rank first.
    assert hits[0]["page_no"] == 1 and "Casual leave" in hits[0]["snippet"]
    assert all(h["document_id"] != corpus["beta_doc"] for h in hits)


async def test_search_cannot_cross_tenant(bob, corpus):
    assert (await bob.get(f"/v1/workspaces/{WS_ALPHA_HR}/search",
                          params={"q": "casual leave"})).status_code == 404
    hits = (await bob.get(f"/v1/workspaces/{WS_BETA_LEGAL}/search",
                          params={"q": "casual leave"})).json()
    assert hits and all(h["document_id"] != corpus["alpha_doc"] for h in hits)


# ---------- chat ----------

async def test_chat_answers_with_validated_citation(alice, corpus):
    turn, events = await ask(alice, WS_ALPHA_HR, "How many casual leave days per year?")
    kinds = [e for e, _ in events]
    assert kinds[0] == "started" and kinds[-1] == "completed" and "delta" in kinds
    done = final(events)
    assert "12 days" in done["answer"] and done["grounding"] == "supported"
    cites = [d for e, d in events if e == "citation"]
    # Earlier tests may have uploaded the same policy text: any Alpha copy is a valid source.
    assert cites and cites[0]["page_start"] == 1 and "Casual leave" in cites[0]["quote_preview"]
    assert all(c["document_id"] != corpus["beta_doc"] for c in cites)
    assert "@" not in cites[0]["quote_preview"]  # preview is redacted text
    ids = [int(line.split(": ")[1]) for line in
           (await alice.get(turn["stream_url"])).text.splitlines() if line.startswith("id: ")]
    assert ids == sorted(ids)  # replay has monotonic sequence numbers


async def test_model_context_never_contains_other_tenant_or_raw_pii(alice, corpus, monkeypatch):
    seen = {}

    class Spy(providers.MockChatProvider):
        async def stream_answer(self, **kw):
            seen.update(kw)
            async for p in super().stream_answer(**kw):
                yield p

    monkeypatch.setattr(providers, "get_chat_provider", lambda: Spy())
    await ask(alice, WS_ALPHA_HR, "casual leave days for executives")
    assert "99 days" not in seen["context"] and "Beta confidential" not in seen["context"]
    assert "hr.alpha@alpha.test" not in seen["context"] and "9876543210" not in seen["context"]
    assert "untrusted DATA" in seen["system"]


async def test_hallucinated_citation_never_reaches_client(alice, corpus, monkeypatch):
    class Liar:
        name = "liar"

        async def stream_answer(self, **kw):
            for piece in ["Leave is 12 days [E1] ", "per the CEO memo [E99]."]:
                yield piece

    monkeypatch.setattr(providers, "get_chat_provider", lambda: Liar())
    turn, events = await ask(alice, WS_ALPHA_HR, "casual leave days")
    assert "[E99]" not in final(events)["answer"]
    assert [d["label"] for e, d in events if e == "citation"] == ["E1"]
    conv = (await alice.get(f"/v1/chat/conversations/{turn['conversation_id']}")).json()
    assert [c["label"] for c in conv["turns"][0]["citations"]] == ["E1"]


async def test_no_evidence_means_no_model_call(alice, monkeypatch):
    def boom():
        raise AssertionError("provider must not be called without evidence")

    monkeypatch.setattr(providers, "get_chat_provider", boom)
    empty_ws = uuid.uuid4()
    await admin_sql("INSERT INTO workspaces (id, tenant_id, name) VALUES (:w, :t, :n)",
                    {"w": empty_ws, "t": ALPHA, "n": f"Empty {empty_ws.hex[:6]}"})
    _, events = await ask(alice, empty_ws, "How many casual leave days?")
    done = final(events)
    assert done["grounding"] == "insufficient_evidence"
    assert not [e for e, _ in events if e == "citation"]


async def test_provider_failure_marks_turn_failed(alice, corpus, monkeypatch):
    class Broken:
        name = "broken"

        async def stream_answer(self, **kw):
            raise ConnectionError("provider down")
            yield  # pragma: no cover

    monkeypatch.setattr(providers, "get_chat_provider", lambda: Broken())
    _, events = await ask(alice, WS_ALPHA_HR, "casual leave")
    assert events[-1] == ("failed", {"reason": "generation_failed"})


async def test_deleting_document_leaves_retrieval_and_old_citations(alice, corpus):
    tag = uuid.uuid4().hex[:6]
    t, _ = await upload(alice, WS_ALPHA_HR, f"temp-{tag}.txt",
                        f"Parking allowance code {tag} is 500 rupees.".encode(), TXT)
    await drain()
    turn, events = await ask(alice, WS_ALPHA_HR, f"parking allowance code {tag}")
    assert any(d["document_id"] == t["document_id"] for e, d in events if e == "citation")

    await admin_sql("UPDATE documents SET status = 'deleting' WHERE id = :d",
                    {"d": t["document_id"]})
    hits = (await alice.get(f"/v1/workspaces/{WS_ALPHA_HR}/search",
                            params={"q": f"parking allowance {tag}"})).json()
    assert all(h["document_id"] != t["document_id"] for h in hits)
    conv = (await alice.get(f"/v1/chat/conversations/{turn['conversation_id']}")).json()
    old = [c for c in conv["turns"][0]["citations"] if c["document_id"] == t["document_id"]]
    assert old and old[0]["available"] is False and old[0]["quote_preview"] is None


async def test_chat_history_is_private(alice, carol, bob, corpus):
    turn, _ = await ask(alice, WS_ALPHA_HR, "sick leave")
    for other in (carol, bob):  # same tenant, other tenant
        assert (await other.get(turn["stream_url"])).status_code == 404
        assert (await other.get(
            f"/v1/chat/conversations/{turn['conversation_id']}")).status_code == 404
        assert (await other.post(f"/v1/chat/turns/{turn['turn_id']}/cancel")).status_code == 404


async def test_bob_cannot_chat_in_alpha_workspace(bob):
    r = await bob.post("/v1/chat/turns", json={"workspace_id": str(WS_ALPHA_HR),
                                               "question": "casual leave"})
    assert r.status_code == 404


async def test_cancel_before_stream(alice, corpus):
    r = await alice.post("/v1/chat/turns", json={"workspace_id": str(WS_ALPHA_HR),
                                                 "question": "casual leave"})
    turn = r.json()
    assert (await alice.post(f"/v1/chat/turns/{turn['turn_id']}/cancel")).status_code == 202
    events = parse_sse((await alice.get(turn["stream_url"])).text)
    assert events[-1][0] == "canceled"


async def test_follow_up_turn_in_same_conversation(alice, corpus):
    t1, _ = await ask(alice, WS_ALPHA_HR, "casual leave")
    t2, _ = await ask(alice, WS_ALPHA_HR, "sick leave", t1["conversation_id"])
    assert t2["conversation_id"] == t1["conversation_id"]
    conv = (await alice.get(f"/v1/chat/conversations/{t1['conversation_id']}")).json()
    assert [t["question"] for t in conv["turns"]] == ["casual leave", "sick leave"]


async def test_model_refusal_is_never_reported_as_supported(alice, corpus, monkeypatch):
    class Refuses:
        name = "refuses"

        async def stream_answer(self, **kw):
            yield "NOT_FOUND [E1]"  # a refusal that still "cites" must not look grounded

    monkeypatch.setattr(providers, "get_chat_provider", lambda: Refuses())
    _, events = await ask(alice, WS_ALPHA_HR, "casual leave days")
    done = final(events)
    assert done["grounding"] == "insufficient_evidence"
    assert not [e for e, _ in events if e == "citation"]
    assert "NOT_FOUND" not in done["answer"]


async def test_uncited_answer_is_marked_unverified(alice, corpus, monkeypatch):
    class NoCites:
        name = "nocites"

        async def stream_answer(self, **kw):
            yield "Casual leave is twelve days."

    monkeypatch.setattr(providers, "get_chat_provider", lambda: NoCites())
    _, events = await ask(alice, WS_ALPHA_HR, "casual leave days")
    assert final(events)["grounding"] == "unverified"
    assert not [e for e, _ in events if e == "citation"]
