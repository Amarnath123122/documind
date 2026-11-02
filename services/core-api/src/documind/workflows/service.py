"""Workflow use cases: draft -> human approval (four-eyes) -> export.

Outward-facing output (export) exists only after a *different* authorized person approves,
and only while every source is still live at the version the draft was built from.
"""

import json
import uuid
from collections import defaultdict
from uuid import UUID

from sqlalchemy import text

from documind.common.db import unit_of_work
from documind.common.errors import AppError, Forbidden, NotFound
from documind.documents.repository import DocumentRepository
from documind.identity.domain import Permission, Principal
from documind.workflows.domain import SourceChunk, draft_report


class WorkflowConflict(AppError):
    status, code, title = 409, "WORKFLOW_CONFLICT", "Conflict"


_COLS = ("id, tenant_id, workspace_id, kind, title, status, created_by, draft, sources, "
         "decided_by, decision_note, decided_at, created_at")


async def _get(s, run_id: UUID):
    return (await s.execute(text(f"SELECT {_COLS} FROM workflow_runs WHERE id = :id"),
                            {"id": run_id})).one_or_none()


async def _live_sources(s, sources: list[dict]) -> bool:
    """Every source still visible to the caller, READY, and at the drafted version."""
    ids = [UUID(x["document_id"]) for x in sources]
    live = {(r.id, r.version) for r in await s.execute(text(
        "SELECT id, version FROM documents WHERE id = ANY(:ids) AND status = 'ready'"),
        {"ids": ids})}
    return all((UUID(x["document_id"]), x["document_version"]) in live for x in sources)


async def create(p: Principal, workspace_id: UUID, title: str,
                 document_ids: list[UUID]) -> dict:
    async with unit_of_work(p.user_id) as s:
        repo = DocumentRepository(s)
        ws = await repo.get_workspace(workspace_id)
        if ws is None or not p.can(Permission.WORKSPACE_READ, ws.tenant_id):
            raise NotFound("Workspace not found.")
        if not p.can(Permission.WORKFLOW_RUN, ws.tenant_id):
            raise Forbidden("You do not have permission for this action.",
                            code="WORKFLOW_RUN_DENIED")
        # RLS applies: restricted documents the creator cannot see simply do not exist here.
        docs = {r.id: r for r in await s.execute(text(
            "SELECT id, version, filename FROM documents WHERE id = ANY(:ids) "
            "AND workspace_id = :w AND status = 'ready'"),
            {"ids": document_ids, "w": ws.id})}
        if len(docs) != len(set(document_ids)):
            raise NotFound("One or more documents were not found.", code="DOCUMENT_NOT_FOUND")
        by_doc: dict[UUID, list[SourceChunk]] = defaultdict(list)
        for r in await s.execute(text(
                "SELECT c.document_id, c.document_version, d.filename, c.page_no, c.text "
                "FROM document_chunks c JOIN documents d ON d.id = c.document_id "
                "AND d.version = c.document_version WHERE c.document_id = ANY(:ids) "
                "ORDER BY c.document_id, c.ordinal"), {"ids": list(docs)}):
            by_doc[r.document_id].append(SourceChunk(*r))
        ordered = {d: by_doc.get(d, []) for d in dict.fromkeys(document_ids)}
        sources = [{"document_id": str(d), "document_version": docs[d].version,
                    "filename": docs[d].filename} for d in ordered]
        run_id = uuid.uuid4()
        await s.execute(text(
            "INSERT INTO workflow_runs (id, tenant_id, workspace_id, kind, title, status, "
            "created_by, draft, sources) VALUES (:id, :t, :w, 'compare_documents', :title, "
            "'pending_approval', :by, :draft, CAST(:src AS jsonb))"),
            {"id": run_id, "t": ws.tenant_id, "w": ws.id, "title": title, "by": p.user_id,
             "draft": draft_report(title, ordered), "src": json.dumps(sources)})
        await repo.audit(ws.tenant_id, p.user_id, "workflow.created", run_id)
        return (await _get(s, run_id))._asdict()


async def get(p: Principal, run_id: UUID) -> dict:
    async with unit_of_work(p.user_id) as s:
        run = await _get(s, run_id)  # RLS: creator or tenant admin
    if run is None:
        raise NotFound("Workflow not found.")
    return run._asdict()


async def list_runs(p: Principal, workspace_id: UUID) -> list[dict]:
    async with unit_of_work(p.user_id) as s:
        rows = await s.execute(text(
            f"SELECT {_COLS} FROM workflow_runs WHERE workspace_id = :w "
            "ORDER BY created_at DESC LIMIT 50"), {"w": workspace_id})
        return [r._asdict() for r in rows]


async def decide(p: Principal, run_id: UUID, approve: bool, note: str | None) -> dict:
    async with unit_of_work(p.user_id) as s:
        run = await _get(s, run_id)
        if run is None:
            raise NotFound("Workflow not found.")
        if not p.can(Permission.WORKFLOW_APPROVE, run.tenant_id):
            raise Forbidden("Only an approver can decide.", code="WORKFLOW_APPROVE_DENIED")
        if run.created_by == p.user_id:
            raise Forbidden("The author cannot approve their own report.", code="FOUR_EYES")
        if run.status != "pending_approval":
            raise WorkflowConflict(f"Workflow is already {run.status}.")
        if approve and not await _live_sources(s, run.sources):
            raise WorkflowConflict("A source document changed or was removed; redraft.",
                                   code="SOURCES_CHANGED")
        status = "approved" if approve else "rejected"
        await s.execute(text(
            "UPDATE workflow_runs SET status = :st, decided_by = :by, decision_note = :n, "
            "decided_at = now() WHERE id = :id AND status = 'pending_approval'"),
            {"st": status, "by": p.user_id, "n": note, "id": run_id})
        await DocumentRepository(s).audit(run.tenant_id, p.user_id, f"workflow.{status}",
                                          run_id)
        return (await _get(s, run_id))._asdict()


async def export(p: Principal, run_id: UUID) -> tuple[str, str]:
    async with unit_of_work(p.user_id) as s:
        run = await _get(s, run_id)
        if run is None:
            raise NotFound("Workflow not found.")
        if run.status != "approved":
            raise WorkflowConflict("Only approved reports can be exported.", code="NOT_APPROVED")
        if not await _live_sources(s, run.sources):
            raise WorkflowConflict("A source was deleted or changed after approval.",
                                   code="SOURCES_CHANGED")
        await DocumentRepository(s).audit(run.tenant_id, p.user_id, "workflow.exported", run_id)
    return run.title, run.draft
