from uuid import UUID

from fastapi import APIRouter

from documind.common.db import unit_of_work
from documind.common.errors import NotFound
from documind.identity.domain import Permission
from documind.identity.session import CurrentPrincipal
from documind.usage import service

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/tenants/{tenant_id}/usage")
async def tenant_usage(p: CurrentPrincipal, tenant_id: UUID) -> dict:
    if not p.can(Permission.USAGE_VIEW, tenant_id):
        raise NotFound("Tenant not found.")
    async with unit_of_work(p.user_id) as s:
        return await service.summary(s, tenant_id)
