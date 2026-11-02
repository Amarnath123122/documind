"""Phase 0: identity, tenancy, documents, audit, with RLS.

Revision ID: 0001
"""

from alembic import op

revision = "0001"
down_revision = None

UPGRADE = r"""
CREATE FUNCTION app_user_id() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;

CREATE TABLE tenants (
  id uuid PRIMARY KEY,
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id uuid PRIMARY KEY,
  email text NOT NULL UNIQUE,
  display_name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memberships (
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  user_id uuid NOT NULL REFERENCES users(id),
  role text NOT NULL CHECK (role IN ('tenant_admin','workspace_editor','viewer')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, user_id)
);
CREATE INDEX idx_memberships_user ON memberships (user_id, tenant_id);

CREATE TABLE workspaces (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id)
);

CREATE TABLE documents (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  filename text NOT NULL CHECK (length(filename) BETWEEN 1 AND 255),
  status text NOT NULL,
  version integer NOT NULL DEFAULT 1,
  created_by uuid NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  -- Composite FK: a document can never point at another tenant's workspace.
  FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces (tenant_id, id)
);
CREATE INDEX idx_documents_ws_keyset ON documents (tenant_id, workspace_id, created_at DESC, id DESC);

CREATE TABLE audit_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  actor_user_id uuid NOT NULL,
  action text NOT NULL,
  target_id uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Tenants visible to the current principal. SECURITY DEFINER avoids policy recursion;
-- it still only answers for app_user_id(), which comes from the verified session.
CREATE FUNCTION app_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = documind AS
$$ SELECT tenant_id FROM memberships WHERE user_id = app_user_id() $$;

-- Dev/OIDC login resolution: maps a verified identity to a user id without granting
-- the app role broad SELECT on users.
CREATE FUNCTION resolve_user_by_email(p_email text) RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = documind AS
$$ SELECT id FROM users WHERE lower(email) = lower(p_email) $$;

ALTER TABLE tenants      ENABLE ROW LEVEL SECURITY; ALTER TABLE tenants      FORCE ROW LEVEL SECURITY;
ALTER TABLE users        ENABLE ROW LEVEL SECURITY; ALTER TABLE users        FORCE ROW LEVEL SECURITY;
ALTER TABLE memberships  ENABLE ROW LEVEL SECURITY; ALTER TABLE memberships  FORCE ROW LEVEL SECURITY;
ALTER TABLE workspaces   ENABLE ROW LEVEL SECURITY; ALTER TABLE workspaces   FORCE ROW LEVEL SECURITY;
ALTER TABLE documents    ENABLE ROW LEVEL SECURITY; ALTER TABLE documents    FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY; ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;

CREATE POLICY tenants_member ON tenants USING (id IN (SELECT app_tenant_ids()));
CREATE POLICY users_self ON users USING (id = app_user_id());
-- Only the owner (i.e. the SECURITY DEFINER resolver) may look users up by email.
CREATE POLICY users_resolver ON users FOR SELECT TO documind_owner USING (true);
CREATE POLICY memberships_self ON memberships USING (user_id = app_user_id());
CREATE POLICY workspaces_member ON workspaces
  USING (tenant_id IN (SELECT app_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT app_tenant_ids()));
CREATE POLICY documents_member ON documents
  USING (tenant_id IN (SELECT app_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT app_tenant_ids()));
CREATE POLICY audit_member ON audit_events
  USING (tenant_id IN (SELECT app_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT app_tenant_ids()) AND actor_user_id = app_user_id());

-- Least privilege for the runtime role. Audit is append-only (no UPDATE/DELETE).
GRANT EXECUTE ON FUNCTION app_user_id(), app_tenant_ids(), resolve_user_by_email(text) TO documind_app;
GRANT SELECT ON tenants, users, memberships, workspaces TO documind_app;
GRANT SELECT, INSERT, UPDATE ON documents TO documind_app;
GRANT SELECT, INSERT ON audit_events TO documind_app;
"""

DOWNGRADE = r"""
DROP TABLE audit_events, documents, workspaces, memberships, users, tenants CASCADE;
DROP FUNCTION IF EXISTS resolve_user_by_email(text), app_tenant_ids(), app_user_id();
"""


def _statements(sql: str) -> list[str]:
    # asyncpg runs one statement per call; split on ';' outside $$-quoted bodies.
    out, buf, in_dollar = [], [], False
    for i, ch in enumerate(sql):
        if sql.startswith("$$", i):
            in_dollar = not in_dollar if sql[i - 1 : i] != "$" else in_dollar
        if ch == ";" and not in_dollar:
            stmt = "".join(buf).strip()
            if stmt and not all(ln.strip().startswith("--") for ln in stmt.splitlines()):
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
    return out


def upgrade() -> None:
    for stmt in _statements(UPGRADE):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _statements(DOWNGRADE):
        op.execute(stmt)
