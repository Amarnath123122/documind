"""Postgres adapter. All queries run under RLS; tenant scoping here is defense in depth."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from documind.documents.domain import Document, DocumentStatus

_COLS = ("id, tenant_id, workspace_id, filename, status, version, created_by, created_at, "
         "mime, size_bytes, page_count, failure_reason, visibility")


def _to_doc(r) -> Document:
    return Document(r.id, r.tenant_id, r.workspace_id, r.filename, DocumentStatus(r.status),
                    r.version, r.created_by, r.created_at, r.mime, r.size_bytes,
                    r.page_count, r.failure_reason, r.visibility)


@dataclass(frozen=True)
class WorkspaceRow:
    id: UUID
    tenant_id: UUID
    name: str


@dataclass(frozen=True)
class UploadSession:
    id: UUID
    tenant_id: UUID
    document_id: UUID
    blob_path: str
    declared_size: int
    declared_mime: str
    status: str
    expires_at: datetime


_UCOLS = "id, tenant_id, document_id, blob_path, declared_size, declared_mime, status, expires_at"


class DocumentRepository:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def list_workspaces(self) -> list[WorkspaceRow]:
        rows = await self.s.execute(
            text("SELECT id, tenant_id, name FROM workspaces ORDER BY name, id"))
        return [WorkspaceRow(r.id, r.tenant_id, r.name) for r in rows]

    async def get_workspace(self, workspace_id: UUID) -> WorkspaceRow | None:
        r = (await self.s.execute(
            text("SELECT id, tenant_id, name FROM workspaces WHERE id = :id"),
            {"id": workspace_id})).one_or_none()
        return WorkspaceRow(r.id, r.tenant_id, r.name) if r else None

    async def get(self, document_id: UUID) -> Document | None:
        r = (await self.s.execute(
            text(f"SELECT {_COLS} FROM documents WHERE id = :id"),
            {"id": document_id})).one_or_none()
        return _to_doc(r) if r else None

    async def list_page(self, tenant_id: UUID, workspace_id: UUID, limit: int,
                        after: tuple[datetime, UUID] | None) -> list[Document]:
        """Keyset pagination on (created_at, id) DESC; filters applied before LIMIT."""
        sql = (f"SELECT {_COLS} FROM documents "
               "WHERE tenant_id = :t AND workspace_id = :w AND status <> 'deleted' ")
        params: dict = {"t": tenant_id, "w": workspace_id, "lim": limit}
        if after:
            sql += "AND (created_at, id) < (:ts, :cid) "
            params |= {"ts": after[0], "cid": after[1]}
        sql += "ORDER BY created_at DESC, id DESC LIMIT :lim"
        return [_to_doc(r) for r in await self.s.execute(text(sql), params)]

    async def insert(self, doc: Document) -> Document:
        r = (await self.s.execute(text(
            "INSERT INTO documents (id, tenant_id, workspace_id, filename, status, version, "
            "created_by, mime, size_bytes) VALUES (:id, :t, :w, :f, :st, :v, :by, :m, :sz) "
            "RETURNING " + _COLS),
            {"id": doc.id, "t": doc.tenant_id, "w": doc.workspace_id, "f": doc.filename,
             "st": doc.status.value, "v": doc.version, "by": doc.created_by,
             "m": doc.mime, "sz": doc.size_bytes})).one()
        return _to_doc(r)

    async def cas_status(self, doc_id: UUID, expected: DocumentStatus,
                         target: DocumentStatus, **fields) -> bool:
        """Compare-and-set: only moves if still in `expected`. Returns False on a lost race."""
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        sql = ("UPDATE documents SET status = :target, updated_at = now()"
               + (f", {sets}" if sets else "") + " WHERE id = :id AND status = :expected")
        res = await self.s.execute(text(sql), {"id": doc_id, "expected": expected.value,
                                               "target": target.value, **fields})
        return res.rowcount == 1

    async def find_upload_by_key(self, tenant_id: UUID, key: str) -> UploadSession | None:
        # RLS already limits rows to the caller's own uploads.
        r = (await self.s.execute(text(
            f"SELECT {_UCOLS} FROM upload_sessions WHERE tenant_id = :t AND idempotency_key = :k"),
            {"t": tenant_id, "k": key})).one_or_none()
        return UploadSession(*r) if r else None

    async def get_upload(self, upload_id: UUID) -> UploadSession | None:
        r = (await self.s.execute(text(
            f"SELECT {_UCOLS} FROM upload_sessions WHERE id = :id FOR UPDATE"),
            {"id": upload_id})).one_or_none()
        return UploadSession(*r) if r else None

    async def insert_upload(self, u: UploadSession, created_by: UUID, key: str | None) -> None:
        await self.s.execute(text(
            "INSERT INTO upload_sessions (id, tenant_id, document_id, blob_path, declared_size, "
            "declared_mime, status, idempotency_key, created_by, expires_at) VALUES "
            "(:id, :t, :d, :p, :sz, :m, :st, :k, :by, :exp)"),
            {"id": u.id, "t": u.tenant_id, "d": u.document_id, "p": u.blob_path,
             "sz": u.declared_size, "m": u.declared_mime, "st": u.status, "k": key,
             "by": created_by, "exp": u.expires_at})

    async def set_upload_status(self, upload_id: UUID, status: str) -> None:
        await self.s.execute(text("UPDATE upload_sessions SET status = :s WHERE id = :id"),
                             {"s": status, "id": upload_id})

    async def enqueue(self, tenant_id: UUID, kind: str, doc_id: UUID, version: int) -> None:
        # Unique (kind, document_id, version): a retried "complete" never double-enqueues.
        await self.s.execute(text(
            "INSERT INTO jobs (tenant_id, kind, document_id, document_version) "
            "VALUES (:t, :k, :d, :v) ON CONFLICT DO NOTHING"),
            {"t": tenant_id, "k": kind, "d": doc_id, "v": version})

    async def audit(self, tenant_id: UUID, actor: UUID, action: str, target: UUID) -> None:
        await self.s.execute(text(
            "INSERT INTO audit_events (tenant_id, actor_user_id, action, target_id) "
            "VALUES (:t, :a, :act, :tg)"),
            {"t": tenant_id, "a": actor, "act": action, "tg": target})

    async def mark_deleting(self, doc_id: UUID, version: int) -> int | None:
        return (await self.s.execute(text(
            "UPDATE documents SET status = 'deleting', version = version + 1, updated_at = now() "
            "WHERE id = :id AND version = :v AND status NOT IN ('deleting', 'deleted') "
            "RETURNING version"), {"id": doc_id, "v": version})).scalar_one_or_none()

    async def set_visibility(self, doc_id: UUID, visibility: str) -> None:
        await self.s.execute(text(
            "UPDATE documents SET visibility = :vis, updated_at = now() WHERE id = :id"),
            {"vis": visibility, "id": doc_id})

    async def resolve_member(self, tenant_id: UUID, email: str) -> UUID | None:
        return (await self.s.execute(text("SELECT resolve_member_by_email(:t, :e)"),
                                     {"t": tenant_id, "e": email})).scalar_one_or_none()

    async def add_grant(self, tenant_id: UUID, doc_id: UUID, grantee: UUID, by: UUID) -> None:
        await self.s.execute(text(
            "INSERT INTO sharing_grants (tenant_id, document_id, grantee_user_id, granted_by) "
            "VALUES (:t, :d, :g, :by) "
            "ON CONFLICT (document_id, grantee_user_id) WHERE revoked_at IS NULL DO NOTHING"),
            {"t": tenant_id, "d": doc_id, "g": grantee, "by": by})

    async def revoke_grant(self, doc_id: UUID, grantee: UUID) -> bool:
        res = await self.s.execute(text(
            "UPDATE sharing_grants SET revoked_at = now() WHERE document_id = :d "
            "AND grantee_user_id = :g AND revoked_at IS NULL"), {"d": doc_id, "g": grantee})
        return res.rowcount == 1

    async def list_grants(self, doc_id: UUID) -> list[dict]:
        rows = await self.s.execute(text(
            "SELECT grantee_user_id, created_at FROM sharing_grants "
            "WHERE document_id = :d AND revoked_at IS NULL ORDER BY created_at"), {"d": doc_id})
        return [r._asdict() for r in rows]
