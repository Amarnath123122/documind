import base64
import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, Field, field_validator

from documind.common.errors import BadRequest
from documind.documents import service
from documind.documents.domain import Document
from documind.identity.session import CurrentPrincipal

router = APIRouter(prefix="/v1", tags=["documents"])


class WorkspaceOut(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str


class DocumentOut(BaseModel):
    id: UUID
    workspace_id: UUID
    filename: str
    status: str
    version: int
    created_at: datetime
    mime: str | None
    size_bytes: int | None
    page_count: int | None
    failure_reason: str | None
    visibility: str

    @classmethod
    def of(cls, d: Document) -> "DocumentOut":
        return cls(id=d.id, workspace_id=d.workspace_id, filename=d.filename,
                   status=d.status, version=d.version, created_at=d.created_at, mime=d.mime,
                   size_bytes=d.size_bytes, page_count=d.page_count,
                   failure_reason=d.failure_reason, visibility=d.visibility)


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    next_cursor: str | None


class CreateUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    mime: str = Field(max_length=127)

    @field_validator("filename")
    @classmethod
    def _no_paths(cls, v: str) -> str:
        if any(c in v for c in "/\\\x00") or v.strip() in {"", ".", ".."}:
            raise ValueError("filename must be a plain file name")
        return v.strip()


class UploadOut(BaseModel):
    upload_id: UUID
    document_id: UUID
    upload_url: str
    required_headers: dict[str, str]
    expires_at: datetime
    max_bytes: int


def _encode_cursor(d: Document) -> str:
    raw = json.dumps([d.created_at.isoformat(), str(d.id)]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(c: str) -> tuple[datetime, UUID]:
    try:
        ts, cid = json.loads(base64.urlsafe_b64decode(c.encode()))
        return datetime.fromisoformat(ts), UUID(cid)
    except (ValueError, TypeError):
        raise BadRequest("Invalid cursor.", code="INVALID_CURSOR") from None


@router.get("/workspaces", response_model=list[WorkspaceOut])
async def list_workspaces(p: CurrentPrincipal):
    return [WorkspaceOut(id=w.id, tenant_id=w.tenant_id, name=w.name)
            for w in await service.list_workspaces(p)]


@router.get("/workspaces/{workspace_id}/documents", response_model=DocumentPage)
async def list_documents(p: CurrentPrincipal, workspace_id: UUID,
                         limit: Annotated[int, Query(ge=1, le=100)] = 25,
                         cursor: str | None = None):
    after = _decode_cursor(cursor) if cursor else None
    docs = await service.list_documents(p, workspace_id, limit + 1, after)
    page = docs[:limit]
    nxt = _encode_cursor(page[-1]) if len(docs) > limit else None
    return DocumentPage(items=[DocumentOut.of(d) for d in page], next_cursor=nxt)


@router.post("/workspaces/{workspace_id}/uploads", response_model=UploadOut, status_code=201)
async def create_upload(p: CurrentPrincipal, workspace_id: UUID, body: CreateUpload,
                        idempotency_key: Annotated[str | None, Header(max_length=128)] = None):
    t = await service.create_upload(p, workspace_id, body.filename, body.size_bytes,
                                    body.mime, idempotency_key)
    return UploadOut(upload_id=t.upload_id, document_id=t.document_id, upload_url=t.upload_url,
                     required_headers={"x-ms-blob-type": "BlockBlob"},
                     expires_at=t.expires_at, max_bytes=t.max_bytes)


@router.post("/uploads/{upload_id}/complete", response_model=DocumentOut, status_code=202)
async def complete_upload(p: CurrentPrincipal, upload_id: UUID):
    return DocumentOut.of(await service.complete_upload(p, upload_id))


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(p: CurrentPrincipal, document_id: UUID):
    return DocumentOut.of(await service.get_document(p, document_id))


class VisibilityIn(BaseModel):
    visibility: Literal["workspace", "restricted"]


class GrantIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class GrantOut(BaseModel):
    grantee_user_id: UUID
    created_at: datetime


@router.delete("/documents/{document_id}", response_model=DocumentOut, status_code=202)
async def delete_document(p: CurrentPrincipal, document_id: UUID,
                          if_match: Annotated[str | None, Header()] = None):
    try:
        version = int(if_match.strip('W/"')) if if_match else None
    except ValueError:
        raise BadRequest("If-Match must be a document version.") from None
    return DocumentOut.of(await service.delete_document(p, document_id, version))


@router.patch("/documents/{document_id}", response_model=DocumentOut)
async def update_document(p: CurrentPrincipal, document_id: UUID, body: VisibilityIn):
    return DocumentOut.of(await service.set_visibility(p, document_id, body.visibility))


@router.get("/documents/{document_id}/grants", response_model=list[GrantOut])
async def list_grants(p: CurrentPrincipal, document_id: UUID):
    return await service.list_grants(p, document_id)


@router.post("/documents/{document_id}/grants", status_code=204)
async def add_grant(p: CurrentPrincipal, document_id: UUID, body: GrantIn) -> None:
    await service.add_grant(p, document_id, body.email)


@router.delete("/documents/{document_id}/grants/{user_id}", status_code=204)
async def revoke_grant(p: CurrentPrincipal, document_id: UUID, user_id: UUID) -> None:
    await service.revoke_grant(p, document_id, user_id)
