import re
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from documind.identity.session import CurrentPrincipal
from documind.workflows import service

router = APIRouter(prefix="/v1/workflows", tags=["workflows"])


class CreateWorkflow(BaseModel):
    workspace_id: UUID
    title: str = Field(min_length=1, max_length=200)
    document_ids: Annotated[list[UUID], Field(min_length=1, max_length=10)]


class Decision(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class WorkflowOut(BaseModel):
    id: UUID
    workspace_id: UUID
    kind: str
    title: str
    status: str
    created_by: UUID
    draft: str
    sources: list[dict[str, Any]]
    decided_by: UUID | None
    decision_note: str | None
    decided_at: datetime | None
    created_at: datetime


@router.post("", response_model=WorkflowOut, status_code=201)
async def create(p: CurrentPrincipal, body: CreateWorkflow):
    return await service.create(p, body.workspace_id, body.title.strip(), body.document_ids)


@router.get("", response_model=list[WorkflowOut])
async def list_runs(p: CurrentPrincipal, workspace_id: UUID):
    return await service.list_runs(p, workspace_id)


@router.get("/{run_id}", response_model=WorkflowOut)
async def get(p: CurrentPrincipal, run_id: UUID):
    return await service.get(p, run_id)


@router.post("/{run_id}/approve", response_model=WorkflowOut)
async def approve(p: CurrentPrincipal, run_id: UUID, body: Decision):
    return await service.decide(p, run_id, True, body.note)


@router.post("/{run_id}/reject", response_model=WorkflowOut)
async def reject(p: CurrentPrincipal, run_id: UUID, body: Decision):
    return await service.decide(p, run_id, False, body.note)


@router.get("/{run_id}/export", response_class=PlainTextResponse)
async def export(p: CurrentPrincipal, run_id: UUID):
    title, body = await service.export(p, run_id)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-")[:80] or "report"
    return PlainTextResponse(body, media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{safe}.md"'})
