"""Synthetic two-tenant seed for local dev and tests. Idempotent.

Runs as the Postgres superuser (local only) because FORCE RLS blocks the owner from
inserting rows for tenants it is not a member of. Never runs in production.
"""

import asyncio
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from documind.common.config import get_settings
from documind.common.storage import get_storage

ALPHA = UUID("a0000000-0000-4000-8000-000000000001")
BETA = UUID("b0000000-0000-4000-8000-000000000001")
WS_ALPHA_HR = UUID("a0000000-0000-4000-8000-0000000000a1")
WS_BETA_LEGAL = UUID("b0000000-0000-4000-8000-0000000000b1")
ALICE = UUID("00000000-0000-4000-8000-00000000a11c")  # Alpha tenant_admin
CAROL = UUID("00000000-0000-4000-8000-00000000ca01")  # Alpha viewer
BOB = UUID("00000000-0000-4000-8000-000000000b0b")    # Beta workspace_editor
DAVE = UUID("00000000-0000-4000-8000-00000000da7e")   # Alpha workspace_editor

TENANTS = [(ALPHA, "Company Alpha"), (BETA, "Company Beta")]
USERS = [(ALICE, "alice@alpha.test", "Alice (Alpha admin)"),
         (CAROL, "carol@alpha.test", "Carol (Alpha viewer)"),
         (BOB, "bob@beta.test", "Bob (Beta editor)"),
         (DAVE, "dave@alpha.test", "Dave (Alpha editor)")]
MEMBERSHIPS = [(ALPHA, ALICE, "tenant_admin"), (ALPHA, CAROL, "viewer"),
               (BETA, BOB, "workspace_editor"), (ALPHA, DAVE, "workspace_editor")]
WORKSPACES = [(WS_ALPHA_HR, ALPHA, "Alpha HR"), (WS_BETA_LEGAL, BETA, "Beta Legal")]
DOCS = [(UUID("d0000000-0000-4000-8000-00000000a001"), ALPHA, WS_ALPHA_HR,
         "employment-policy.pdf", ALICE),
        (UUID("d0000000-0000-4000-8000-00000000b001"), BETA, WS_BETA_LEGAL,
         "beta-secret-contract.pdf", BOB)]


async def seed() -> None:
    s = get_settings()
    if s.env == "production":
        raise SystemExit("refusing to seed production")
    engine = create_async_engine(s.seed_database_url)
    async with engine.begin() as c:
        await c.execute(text("SET search_path = documind"))
        for i, n in TENANTS:
            await c.execute(text("INSERT INTO tenants (id, name) VALUES (:i, :n) "
                                 "ON CONFLICT DO NOTHING"), {"i": i, "n": n})
        for i, e, d in USERS:
            await c.execute(text("INSERT INTO users (id, email, display_name) VALUES "
                                 "(:i, :e, :d) ON CONFLICT DO NOTHING"),
                            {"i": i, "e": e, "d": d})
        for t, u, r in MEMBERSHIPS:
            await c.execute(text("INSERT INTO memberships (tenant_id, user_id, role) VALUES "
                                 "(:t, :u, :r) ON CONFLICT DO NOTHING"),
                            {"t": t, "u": u, "r": r})
        for i, t, n in WORKSPACES:
            await c.execute(text("INSERT INTO workspaces (id, tenant_id, name) VALUES "
                                 "(:i, :t, :n) ON CONFLICT DO NOTHING"),
                            {"i": i, "t": t, "n": n})
        for t, _ in TENANTS:
            await c.execute(text(
                "INSERT INTO tenant_quotas (tenant_id, monthly_chat_tokens, storage_bytes) "
                "VALUES (:t, 2000000, 5368709120) ON CONFLICT DO NOTHING"), {"t": t})
        for i, t, w, f, by in DOCS:
            await c.execute(text(
                "INSERT INTO documents (id, tenant_id, workspace_id, filename, status, "
                "created_by) VALUES (:i, :t, :w, :f, 'ready', :by) ON CONFLICT DO NOTHING"),
                {"i": i, "t": t, "w": w, "f": f, "by": by})
    await engine.dispose()
    await get_storage().ensure_containers()
    print("seeded: Company Alpha (alice, carol, dave), Company Beta (bob); blob containers ready")


if __name__ == "__main__":
    asyncio.run(seed())
