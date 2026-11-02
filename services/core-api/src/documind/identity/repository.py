from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from documind.identity.domain import Membership, Principal, Role


async def resolve_user_id(session: AsyncSession, email: str) -> UUID | None:
    return (
        await session.execute(text("SELECT resolve_user_by_email(:e)"), {"e": email})
    ).scalar_one_or_none()


async def resolve_oidc_user(session: AsyncSession, iss: str, sub: str, email: str | None,
                            email_verified: bool) -> UUID | None:
    return (await session.execute(
        text("SELECT resolve_user_by_oidc(:i, :s, :e, :v)"),
        {"i": iss, "s": sub, "e": email, "v": email_verified})).scalar_one_or_none()


async def load_principal(session: AsyncSession, user_id: UUID) -> Principal | None:
    """Must run inside unit_of_work(user_id): RLS limits rows to this user."""
    user = (
        await session.execute(text("SELECT id, email, display_name FROM users"))
    ).one_or_none()
    if user is None or user.id != user_id:
        return None
    rows = await session.execute(
        text(
            "SELECT m.tenant_id, t.name, m.role FROM memberships m "
            "JOIN tenants t ON t.id = m.tenant_id ORDER BY t.name"
        )
    )
    return Principal(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        memberships=tuple(Membership(r.tenant_id, r.name, Role(r.role)) for r in rows),
    )
