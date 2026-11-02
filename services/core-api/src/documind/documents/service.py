"""Use cases. Authorization decisions live here, never in the UI or an LLM."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from documind.common.config import get_settings
from documind.common.db import unit_of_work
from documind.common.errors import AppError, Forbidden, NotFound
from documind.common.storage import QUARANTINE, BlobInfo, get_storage
from documind.documents.content import ALLOWED_MIME, HEAD_BYTES, bytes_match_declared
from documind.documents.domain import Document, DocumentStatus, transition
from documind.documents.repository import DocumentRepository, UploadSession, WorkspaceRow
from documind.identity.domain import Permission, Principal
from documind.usage import service as usage

INGEST = "ingest"
CLEANUP = "cleanup"


class UploadRejected(AppError):
    status, code, title = 422, "UPLOAD_REJECTED", "Upload Rejected"


class PayloadTooLarge(AppError):
    status, code, title = 413, "UPLOAD_TOO_LARGE", "Payload Too Large"


class Conflict(AppError):
    status, code, title = 409, "CONFLICT", "Conflict"


@dataclass(frozen=True)
class UploadTicket:
    upload_id: UUID
    document_id: UUID
    upload_url: str
    expires_at: datetime
    max_bytes: int


async def list_workspaces(p: Principal) -> list[WorkspaceRow]:
    async with unit_of_work(p.user_id) as s:
        rows = await DocumentRepository(s).list_workspaces()
    return [w for w in rows if p.can(Permission.WORKSPACE_READ, w.tenant_id)]


async def _visible_workspace(repo: DocumentRepository, p: Principal, ws_id: UUID) -> WorkspaceRow:
    # The client-supplied ID is only a selector (invariant 1). Unknown and foreign look identical.
    ws = await repo.get_workspace(ws_id)
    if ws is None or not p.can(Permission.WORKSPACE_READ, ws.tenant_id):
        raise NotFound("Workspace not found.")
    return ws


async def list_documents(p: Principal, ws_id: UUID, limit: int,
                         after: tuple[datetime, UUID] | None) -> list[Document]:
    async with unit_of_work(p.user_id) as s:
        repo = DocumentRepository(s)
        ws = await _visible_workspace(repo, p, ws_id)
        if not p.can(Permission.DOCUMENT_READ, ws.tenant_id):
            raise NotFound("Workspace not found.")
        return await repo.list_page(ws.tenant_id, ws.id, limit, after)


async def get_document(p: Principal, doc_id: UUID) -> Document:
    async with unit_of_work(p.user_id) as s:
        doc = await DocumentRepository(s).get(doc_id)
    if doc is None or doc.status == DocumentStatus.DELETED \
            or not p.can(Permission.DOCUMENT_READ, doc.tenant_id):
        raise NotFound("Document not found.")
    return doc


async def create_upload(p: Principal, ws_id: UUID, filename: str, size_bytes: int,
                        mime: str, idempotency_key: str | None) -> UploadTicket:
    cfg = get_settings()
    if mime not in ALLOWED_MIME:
        raise UploadRejected(f"Unsupported type: {mime}.", code="UNSUPPORTED_TYPE")
    if size_bytes > cfg.max_upload_bytes:
        raise PayloadTooLarge(f"Maximum size is {cfg.max_upload_bytes} bytes.")
    storage = get_storage()
    async with unit_of_work(p.user_id) as s:
        repo = DocumentRepository(s)
        ws = await _visible_workspace(repo, p, ws_id)
        if not p.can(Permission.DOCUMENT_CREATE, ws.tenant_id):
            raise Forbidden("You do not have permission for this action.",
                            code="DOCUMENT_CREATE_DENIED")
        await usage.check(s, ws.tenant_id, usage.STORAGE_BYTES, size_bytes)
        if idempotency_key and (prev := await repo.find_upload_by_key(ws.tenant_id,
                                                                     idempotency_key)):
            # Same request retried: same document, freshly minted capability.
            return await _ticket(storage, prev, cfg.upload_ttl_s, cfg.max_upload_bytes)

        doc_id = uuid.uuid4()
        await repo.insert(Document(
            id=doc_id, tenant_id=ws.tenant_id, workspace_id=ws.id, filename=filename,
            status=transition(DocumentStatus.CREATED, DocumentStatus.UPLOADING), version=1,
            created_by=p.user_id, created_at=datetime.now(UTC), mime=mime))
        # Path is server-chosen and contains no user input.
        upload = UploadSession(
            id=uuid.uuid4(), tenant_id=ws.tenant_id, document_id=doc_id,
            blob_path=f"{ws.tenant_id}/{doc_id}/v1", declared_size=size_bytes,
            declared_mime=mime, status="pending",
            expires_at=datetime.now(UTC) + timedelta(seconds=cfg.upload_ttl_s))
        await repo.insert_upload(upload, p.user_id, idempotency_key)
        await repo.audit(ws.tenant_id, p.user_id, "document.upload_started", doc_id)
    return await _ticket(storage, upload, cfg.upload_ttl_s, cfg.max_upload_bytes)


async def _ticket(storage, u: UploadSession, ttl_s: int, max_bytes: int) -> UploadTicket:
    url = await storage.upload_url(QUARANTINE, u.blob_path, ttl_s)
    return UploadTicket(u.id, u.document_id, url, u.expires_at, max_bytes)


async def complete_upload(p: Principal, upload_id: UUID) -> Document:
    """Verify the bytes actually stored, then quarantine + enqueue in ONE transaction.

    Idempotent: completing twice returns the current document state.
    """
    storage = get_storage()
    async with unit_of_work(p.user_id) as s:
        repo = DocumentRepository(s)
        upload = await repo.get_upload(upload_id)  # RLS: only the uploader sees it
        if upload is None:
            raise NotFound("Upload not found.")
        if upload.status == "completed":
            return await repo.get(upload.document_id)
        if upload.status == "failed":
            raise Conflict("Upload already failed; start a new upload.", code="UPLOAD_FAILED")
        if upload.expires_at < datetime.now(UTC):
            raise Conflict("Upload session expired.", code="UPLOAD_EXPIRED")

        info = await storage.info(QUARANTINE, upload.blob_path)
        if info is None:
            raise Conflict("No bytes have been uploaded yet.", code="UPLOAD_MISSING")
        head = await storage.read_head(QUARANTINE, upload.blob_path, HEAD_BYTES)
        reason = None
        if info.size != upload.declared_size:
            reason = "size_mismatch"
        elif not bytes_match_declared(upload.declared_mime, head):
            reason = "content_type_mismatch"

        if reason:
            # Record the rejection (commit), then raise outside the transaction.
            await repo.set_upload_status(upload.id, "failed")
            await repo.cas_status(upload.document_id, DocumentStatus.UPLOADING,
                                  DocumentStatus.FAILED, failure_reason=reason)
            await repo.audit(upload.tenant_id, p.user_id, f"document.rejected.{reason}",
                             upload.document_id)
        else:
            return await _quarantine_and_enqueue(repo, p, upload, info)
    await storage.delete(QUARANTINE, upload.blob_path)
    raise UploadRejected("Uploaded file failed verification.", code=reason.upper())


async def _quarantine_and_enqueue(repo: DocumentRepository, p: Principal,
                                  upload: UploadSession, info: BlobInfo) -> Document:
    # The etag pins exactly these bytes; the worker refuses a blob overwritten later.
    moved = await repo.cas_status(
        upload.document_id, DocumentStatus.UPLOADING, DocumentStatus.QUARANTINED,
        size_bytes=info.size, blob_etag=info.etag)
    if not moved:
        raise Conflict("Document is no longer accepting uploads.")
    await repo.set_upload_status(upload.id, "completed")
    await usage.record(repo.s, upload.tenant_id, p.user_id, usage.STORAGE_BYTES, info.size,
                       upload.document_id)
    await repo.enqueue(upload.tenant_id, INGEST, upload.document_id, 1)
    await repo.audit(upload.tenant_id, p.user_id, "document.upload_completed",
                     upload.document_id)
    return await repo.get(upload.document_id)


class PreconditionFailed(AppError):
    status, code, title = 412, "PRECONDITION_FAILED", "Precondition Failed"


async def _manageable(repo: DocumentRepository, p: Principal, doc_id: UUID,
                      permission: Permission) -> Document:
    """Visible (RLS) + (tenant admin, or the creator holding `permission`)."""
    doc = await repo.get(doc_id)
    if doc is None or doc.status == DocumentStatus.DELETED \
            or not p.can(Permission.DOCUMENT_READ, doc.tenant_id):
        raise NotFound("Document not found.")
    admin = p.can(Permission.DOCUMENT_DELETE, doc.tenant_id)  # tenant_admin only
    if not (admin or (doc.created_by == p.user_id and p.can(permission, doc.tenant_id))):
        raise Forbidden("You do not have permission for this action.",
                        code="DOCUMENT_MANAGE_DENIED")
    return doc


async def delete_document(p: Principal, doc_id: UUID, if_match: int | None) -> Document:
    """Deny-first: reads/search/chat stop at once; bytes and index are removed by a job.

    Bumping the version makes every in-flight ingest job for the old version abandon itself.
    """
    async with unit_of_work(p.user_id) as s:
        repo = DocumentRepository(s)
        doc = await _manageable(repo, p, doc_id, Permission.DOCUMENT_CREATE)
        if doc.status == DocumentStatus.DELETING:
            return doc  # idempotent
        if if_match is not None and if_match != doc.version:
            raise PreconditionFailed("Document changed since you loaded it.")
        new_version = await repo.mark_deleting(doc.id, doc.version)
        if new_version is None:
            raise Conflict("Document changed concurrently; retry.")
        await repo.enqueue(doc.tenant_id, CLEANUP, doc.id, new_version)
        await repo.audit(doc.tenant_id, p.user_id, "document.delete_requested", doc.id)
        return await repo.get(doc.id)


async def set_visibility(p: Principal, doc_id: UUID, visibility: str) -> Document:
    async with unit_of_work(p.user_id) as s:
        repo = DocumentRepository(s)
        doc = await _manageable(repo, p, doc_id, Permission.DOCUMENT_SHARE)
        await repo.set_visibility(doc.id, visibility)
        await repo.audit(doc.tenant_id, p.user_id, f"document.visibility.{visibility}", doc.id)
        return await repo.get(doc.id)


async def list_grants(p: Principal, doc_id: UUID) -> list[dict]:
    async with unit_of_work(p.user_id) as s:
        repo = DocumentRepository(s)
        await _manageable(repo, p, doc_id, Permission.DOCUMENT_SHARE)
        return await repo.list_grants(doc_id)


async def add_grant(p: Principal, doc_id: UUID, email: str) -> None:
    async with unit_of_work(p.user_id) as s:
        repo = DocumentRepository(s)
        doc = await _manageable(repo, p, doc_id, Permission.DOCUMENT_SHARE)
        grantee = await repo.resolve_member(doc.tenant_id, email)
        if grantee is None:  # unknown or in another tenant: same answer
            raise NotFound("No such member in this organization.", code="MEMBER_NOT_FOUND")
        await repo.add_grant(doc.tenant_id, doc.id, grantee, p.user_id)
        await repo.audit(doc.tenant_id, p.user_id, "document.shared", doc.id)


async def revoke_grant(p: Principal, doc_id: UUID, grantee: UUID) -> None:
    """Effective on the very next query: grants are evaluated inside RLS, never cached."""
    async with unit_of_work(p.user_id) as s:
        repo = DocumentRepository(s)
        doc = await _manageable(repo, p, doc_id, Permission.DOCUMENT_SHARE)
        if not await repo.revoke_grant(doc.id, grantee):
            raise NotFound("Grant not found.")
        await repo.audit(doc.tenant_id, p.user_id, "document.share_revoked", doc.id)
