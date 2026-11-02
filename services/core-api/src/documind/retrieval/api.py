from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel

from documind.common.db import unit_of_work
from documind.common.errors import NotFound
from documind.documents.repository import DocumentRepository
from documind.identity.domain import Permission
from documind.identity.session import CurrentPrincipal
from documind.retrieval.service import hybrid_search

router = APIRouter(prefix="/v1", tags=["search"])


class SearchHit(BaseModel):
    chunk_id: UUID
    document_id: UUID
    filename: str
    page_no: int
    snippet: str


@router.get("/workspaces/{workspace_id}/search", response_model=list[SearchHit])
async def search(p: CurrentPrincipal, workspace_id: UUID,
                 q: Annotated[str, Query(min_length=1, max_length=500)]):
    async with unit_of_work(p.user_id) as s:
        ws = await DocumentRepository(s).get_workspace(workspace_id)
        if ws is None or not p.can(Permission.DOCUMENT_READ, ws.tenant_id):
            raise NotFound("Workspace not found.")
        hits = await hybrid_search(s, tenant_ids=[ws.tenant_id], workspace_ids=[ws.id], query=q)
    return [SearchHit(chunk_id=h.chunk_id, document_id=h.document_id, filename=h.filename,
                      page_no=h.page_no, snippet=h.text[:300]) for h in hits]
