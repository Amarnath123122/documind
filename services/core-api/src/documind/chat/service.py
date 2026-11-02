"""Secure chat turns: retrieve -> recheck -> generate -> validate citations -> persist.

The LLM never decides access. Evidence is gathered under the caller's RLS context and
re-checked immediately before the model call; citations are re-validated server-side.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from documind.chat import providers
from documind.chat.domain import (
    NO_EVIDENCE_ANSWER,
    SYSTEM_PROMPT,
    Evidence,
    Grounding,
    approx_tokens,
    build_context,
    strip_invalid_markers,
    valid_citations,
)
from documind.common.config import get_settings
from documind.common.db import unit_of_work
from documind.common.errors import AppError, NotFound
from documind.common.telemetry import chat_latency, tracer
from documind.documents.repository import DocumentRepository
from documind.identity.domain import Permission, Principal
from documind.retrieval.service import hybrid_search
from documind.usage import service as usage

TERMINAL = ("completed", "failed", "canceled")
CANCEL_CHECK_EVERY = 8  # deltas


class TurnConflict(AppError):
    status, code, title = 409, "TURN_IN_PROGRESS", "Conflict"


@dataclass(frozen=True)
class TurnRef:
    turn_id: UUID
    conversation_id: UUID


def sse(seq: int, event: str, data: dict) -> str:
    return f"id: {seq}\nevent: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def create_turn(p: Principal, workspace_id: UUID, question: str,
                      conversation_id: UUID | None) -> TurnRef:
    async with unit_of_work(p.user_id) as s:
        ws = await DocumentRepository(s).get_workspace(workspace_id)
        if ws is None or not p.can(Permission.DOCUMENT_READ, ws.tenant_id):
            raise NotFound("Workspace not found.")
        if conversation_id:
            conv_ws = (await s.execute(text(
                "SELECT workspace_id FROM chat_conversations WHERE id = :c"),
                {"c": conversation_id})).scalar_one_or_none()  # RLS: own conversations only
            if conv_ws != ws.id:
                raise NotFound("Conversation not found.")
        await usage.check(s, ws.tenant_id, usage.CHAT_TOKENS)
        if not conversation_id:
            conversation_id = uuid.uuid4()
            await s.execute(text(
                "INSERT INTO chat_conversations (id, tenant_id, workspace_id, user_id, title) "
                "VALUES (:id, :t, :w, :u, :title)"),
                {"id": conversation_id, "t": ws.tenant_id, "w": ws.id, "u": p.user_id,
                 "title": question[:80]})
        turn_id = uuid.uuid4()
        await s.execute(text(
            "INSERT INTO chat_turns (id, tenant_id, conversation_id, user_id, question) "
            "VALUES (:id, :t, :c, :u, :q)"),
            {"id": turn_id, "t": ws.tenant_id, "c": conversation_id, "u": p.user_id,
             "q": question})
    return TurnRef(turn_id, conversation_id)


async def cancel_turn(p: Principal, turn_id: UUID) -> None:
    async with unit_of_work(p.user_id) as s:
        res = await s.execute(text(
            "UPDATE chat_turns SET cancel_requested = true, "
            "  status = CASE WHEN status = 'pending' THEN 'canceled' ELSE status END, "
            "  completed_at = CASE WHEN status = 'pending' THEN now() ELSE completed_at END "
            "WHERE id = :id"), {"id": turn_id})
    if res.rowcount != 1:
        raise NotFound("Turn not found.")


async def open_stream(p: Principal, turn_id: UUID) -> AsyncIterator[str]:
    """Validate before the 200 is sent; returns the event generator."""
    async with unit_of_work(p.user_id) as s:
        claimed = (await s.execute(text(
            "UPDATE chat_turns SET status = 'streaming' WHERE id = :id AND status = 'pending' "
            "RETURNING id"), {"id": turn_id})).scalar_one_or_none()
        status = (await s.execute(text("SELECT status FROM chat_turns WHERE id = :id"),
                                  {"id": turn_id})).scalar_one_or_none()
    if status is None:
        raise NotFound("Turn not found.")
    if claimed:
        return _generate(p, turn_id)
    if status in TERMINAL:
        return _replay(p, turn_id)
    raise TurnConflict("This turn is already streaming in another connection.")


async def _turn_scope(p: Principal, turn_id: UUID):
    async with unit_of_work(p.user_id) as s:
        return (await s.execute(text(
            "SELECT t.question, t.tenant_id, c.workspace_id FROM chat_turns t "
            "JOIN chat_conversations c ON c.id = t.conversation_id WHERE t.id = :id"),
            {"id": turn_id})).one()


async def _retrieve(p: Principal, tenant_id: UUID, workspace_id: UUID,
                    question: str) -> list[Evidence]:
    if not p.can(Permission.DOCUMENT_READ, tenant_id):  # principal was loaded fresh this request
        return []
    async with unit_of_work(p.user_id) as s:
        hits = await hybrid_search(s, tenant_ids=[tenant_id], workspace_ids=[workspace_id],
                                   query=question)
    return [Evidence(f"E{i}", h.chunk_id, h.document_id, h.document_version, h.filename,
                     h.page_no, h.char_start, h.char_end, h.text)
            for i, h in enumerate(hits, start=1)]


async def _recheck(p: Principal, evidence: list[Evidence]) -> list[Evidence]:
    """Right before the model call: still READY, same version, still visible to this user."""
    if not evidence:
        return []
    async with unit_of_work(p.user_id) as s:
        live = {(r.id, r.version) for r in await s.execute(text(
            "SELECT id, version FROM documents WHERE id = ANY(:ids) AND status = 'ready'"),
            {"ids": list({e.document_id for e in evidence})})}
    return [e for e in evidence if (e.document_id, e.document_version) in live]


async def _cancel_requested(p: Principal, turn_id: UUID) -> bool:
    async with unit_of_work(p.user_id) as s:
        return bool((await s.execute(text(
            "SELECT cancel_requested FROM chat_turns WHERE id = :id"),
            {"id": turn_id})).scalar_one())


async def _finish(p: Principal, turn_id: UUID, status: str, *, answer: str | None = None,
                  grounding: str | None = None, reason: str | None = None,
                  provider: str | None = None, prompt_tokens: int | None = None,
                  completion_tokens: int | None = None,
                  citations: list[Evidence] = ()) -> None:
    async with unit_of_work(p.user_id) as s:
        row = (await s.execute(text(
            "UPDATE chat_turns SET status = :st, answer = :a, grounding = :g, "
            "failure_reason = :r, provider = :pv, prompt_tokens = :pt, completion_tokens = :ct, "
            "completed_at = now() WHERE id = :id AND status = 'streaming' "
            "RETURNING tenant_id"),
            {"st": status, "a": answer, "g": grounding, "r": reason, "pv": provider,
             "pt": prompt_tokens, "ct": completion_tokens, "id": turn_id})).one_or_none()
        if row and (prompt_tokens or completion_tokens):
            await usage.record(s, row.tenant_id, p.user_id, usage.CHAT_TOKENS,
                               (prompt_tokens or 0) + (completion_tokens or 0), turn_id)
        if row and citations:
            await s.execute(text(
                "INSERT INTO chat_citations (turn_id, tenant_id, user_id, label, ordinal, "
                "chunk_id, document_id, document_version, filename, page_no, char_start, "
                "char_end, quote_preview) VALUES (:turn, :t, :u, :l, :o, :c, :d, :v, :f, :p, "
                ":cs, :ce, :q)"),
                [{"turn": turn_id, "t": row.tenant_id, "u": p.user_id, "l": e.label, "o": i,
                  "c": e.chunk_id, "d": e.document_id, "v": e.document_version,
                  "f": e.filename, "p": e.page_no, "cs": e.char_start, "ce": e.char_end,
                  "q": e.text[:240]} for i, e in enumerate(citations)])


def _citation_payload(e: Evidence | dict) -> dict:
    get = (lambda k: e[k]) if isinstance(e, dict) else (lambda k: getattr(e, k))
    return {"label": get("label"), "document_id": get("document_id"),
            "document_version": get("document_version"), "filename": get("filename"),
            "page_start": get("page_no"), "page_end": get("page_no"),
            "char_start": get("char_start"), "char_end": get("char_end"),
            "quote_preview": get("text") if not isinstance(e, dict) else get("quote_preview")}


async def _generate(p: Principal, turn_id: UUID) -> AsyncIterator[str]:
    cfg = get_settings()
    seq = 0

    def ev(event: str, data: dict) -> str:
        nonlocal seq
        seq += 1
        return sse(seq, event, data)

    finished = False
    span = tracer.start_span("chat.generate", attributes={"turn.id": str(turn_id)})
    started_at = asyncio.get_running_loop().time()
    try:
        yield ev("started", {"turn_id": turn_id})
        q = await _turn_scope(p, turn_id)
        evidence = await _recheck(p, await _retrieve(p, q.tenant_id, q.workspace_id, q.question))
        context, evidence = build_context(evidence, cfg.max_context_chars)
        span.set_attribute("evidence.count", len(evidence))

        if not evidence:
            yield ev("delta", {"text": NO_EVIDENCE_ANSWER})
            await _finish(p, turn_id, "completed", answer=NO_EVIDENCE_ANSWER,
                          grounding=Grounding.INSUFFICIENT)
            finished = True
            yield ev("completed", {"answer": NO_EVIDENCE_ANSWER,
                                   "grounding": Grounding.INSUFFICIENT})
            return

        provider = providers.get_chat_provider()
        parts: list[str] = []
        async with asyncio.timeout(90):
            stream = provider.stream_answer(system=SYSTEM_PROMPT, question=q.question,
                                            context=context,
                                            max_output_tokens=cfg.max_output_tokens)
            async for piece in stream:
                parts.append(piece)
                yield ev("delta", {"text": piece})
                if len(parts) % CANCEL_CHECK_EVERY == 0 and await _cancel_requested(p, turn_id):
                    await _finish(p, turn_id, "canceled", answer="".join(parts),
                                  reason="user_canceled", provider=provider.name)
                    finished = True
                    yield ev("canceled", {})
                    return

        raw = "".join(parts).strip()
        answer = strip_invalid_markers(raw, evidence).strip()
        cited = valid_citations(answer, evidence)
        grounding = Grounding.SUPPORTED if cited else Grounding.INSUFFICIENT
        usage = {"prompt_tokens": approx_tokens(SYSTEM_PROMPT + context + q.question),
                 "completion_tokens": approx_tokens(raw)}
        await _finish(p, turn_id, "completed", answer=answer, grounding=grounding,
                      provider=provider.name, citations=cited, **usage)
        finished = True
        span.set_attributes({"chat.grounding": str(grounding), "chat.citations": len(cited),
                             "llm.provider": provider.name, **{f"llm.{k}": v
                                                               for k, v in usage.items()}})
        for e in cited:
            yield ev("citation", _citation_payload(e) | {"quote_preview": e.text[:240]})
        yield ev("usage", usage)
        yield ev("completed", {"answer": answer, "grounding": grounding})
    except Exception as e:  # noqa: BLE001 - report as a failed turn, never leak details
        if not finished:
            reason = "timeout" if isinstance(e, TimeoutError) else "generation_failed"
            await _finish(p, turn_id, "failed", reason=reason)
            finished = True
            yield ev("failed", {"reason": reason})
    finally:
        chat_latency.record(asyncio.get_running_loop().time() - started_at)
        span.end()
        if not finished:  # client disconnected mid-stream
            try:
                await asyncio.shield(_finish(p, turn_id, "canceled",
                                             reason="client_disconnected"))
            except BaseException:  # noqa: BLE001, S110 - best effort during teardown
                pass


async def _replay(p: Principal, turn_id: UUID) -> AsyncIterator[str]:
    async with unit_of_work(p.user_id) as s:
        t = (await s.execute(text(
            "SELECT status, answer, grounding, failure_reason, prompt_tokens, completion_tokens "
            "FROM chat_turns WHERE id = :id"), {"id": turn_id})).one()
        cites = [r._asdict() for r in await s.execute(text(
            "SELECT label, document_id, document_version, filename, page_no, char_start, "
            "char_end, quote_preview FROM chat_citations WHERE turn_id = :id ORDER BY ordinal"),
            {"id": turn_id})]
    events = [("started", {"turn_id": turn_id, "replay": True}),
                        *([("delta", {"text": t.answer})] if t.answer else []),
                        *[("citation", _citation_payload(c)) for c in cites],
                        ({"completed": ("completed", {"answer": t.answer,
                                                      "grounding": t.grounding}),
                          "canceled": ("canceled", {}),
                          "failed": ("failed", {"reason": t.failure_reason})}[t.status])]
    for seq, (event, data) in enumerate(events, start=1):
        yield sse(seq, event, data)


async def get_conversation(p: Principal, conversation_id: UUID) -> dict:
    async with unit_of_work(p.user_id) as s:
        conv = (await s.execute(text(
            "SELECT id, tenant_id, workspace_id, title, created_at FROM chat_conversations "
            "WHERE id = :id"), {"id": conversation_id})).one_or_none()
        if conv is None or not p.can(Permission.DOCUMENT_READ, conv.tenant_id):
            raise NotFound("Conversation not found.")
        turns = (await s.execute(text(
            "SELECT id, question, answer, status, grounding, created_at FROM chat_turns "
            "WHERE conversation_id = :c ORDER BY created_at"), {"c": conversation_id})).all()
        # Citations are re-validated now: a source deleted or revoked since is not linked.
        cites = (await s.execute(text(
            "SELECT ct.turn_id, ct.label, ct.document_id, ct.document_version, ct.filename, "
            "ct.page_no, ct.char_start, ct.char_end, ct.quote_preview, "
            "(d.status = 'ready' AND d.version = ct.document_version) IS TRUE AS available "
            "FROM chat_citations ct LEFT JOIN documents d ON d.id = ct.document_id "
            "WHERE ct.turn_id = ANY(:ids) ORDER BY ct.turn_id, ct.ordinal"),
            {"ids": [t.id for t in turns]})).all()
    by_turn: dict[UUID, list[dict]] = {}
    for c in cites:
        item = _citation_payload(c._asdict()) | {"available": c.available}
        if not c.available:
            item["quote_preview"] = None
        by_turn.setdefault(c.turn_id, []).append(item)
    return {"id": conv.id, "workspace_id": conv.workspace_id, "title": conv.title,
            "turns": [t._asdict() | {"citations": by_turn.get(t.id, [])} for t in turns]}


async def list_conversations(p: Principal, workspace_id: UUID) -> list[dict]:
    async with unit_of_work(p.user_id) as s:
        rows = await s.execute(text(
            "SELECT id, title, created_at FROM chat_conversations WHERE workspace_id = :w "
            "ORDER BY created_at DESC LIMIT 50"), {"w": workspace_id})
        return [r._asdict() for r in rows]
