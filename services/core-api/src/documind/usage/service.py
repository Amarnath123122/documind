"""Append-only usage ledger + per-tenant quotas. Postgres is the source of truth.

Quotas are checked before work starts (soft limit): concurrent requests may overshoot by
at most one request each. Hard reservations come with billing (ADR-0007).
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from documind.common.errors import AppError

CHAT_TOKENS = "chat_tokens"
STORAGE_BYTES = "storage_bytes"
# Fail closed: a tenant without a quota row gets a small default, not unlimited.
DEFAULT_QUOTA = {CHAT_TOKENS: 50_000, STORAGE_BYTES: 100 * 1024 * 1024}


class QuotaExceeded(AppError):
    status, code, title = 429, "QUOTA_EXCEEDED", "Quota Exceeded"


async def used(s: AsyncSession, tenant_id: UUID, kind: str) -> int:
    window = "AND created_at >= date_trunc('month', now())" if kind == CHAT_TOKENS else ""
    return int((await s.execute(text(
        f"SELECT COALESCE(sum(quantity), 0) FROM usage_ledger "
        f"WHERE tenant_id = :t AND kind = :k {window}"),
        {"t": tenant_id, "k": kind})).scalar_one())


async def limit(s: AsyncSession, tenant_id: UUID, kind: str) -> int:
    col = "monthly_chat_tokens" if kind == CHAT_TOKENS else "storage_bytes"
    v = (await s.execute(text(f"SELECT {col} FROM tenant_quotas WHERE tenant_id = :t"),
                         {"t": tenant_id})).scalar_one_or_none()
    return int(v) if v is not None else DEFAULT_QUOTA[kind]


async def check(s: AsyncSession, tenant_id: UUID, kind: str, extra: int = 0) -> None:
    if await used(s, tenant_id, kind) + extra > await limit(s, tenant_id, kind):
        what = "Monthly AI usage" if kind == CHAT_TOKENS else "Storage"
        raise QuotaExceeded(f"{what} quota for this organization is exhausted.")


async def record(s: AsyncSession, tenant_id: UUID, user_id: UUID | None, kind: str,
                 quantity: int, ref_id: UUID | None) -> None:
    await s.execute(text(
        "INSERT INTO usage_ledger (tenant_id, user_id, kind, quantity, ref_id) "
        "VALUES (:t, :u, :k, :q, :r)"),
        {"t": tenant_id, "u": user_id, "k": kind, "q": quantity, "r": ref_id})


async def summary(s: AsyncSession, tenant_id: UUID) -> dict:
    return {kind: {"used": await used(s, tenant_id, kind),
                   "limit": await limit(s, tenant_id, kind)}
            for kind in (CHAT_TOKENS, STORAGE_BYTES)}
