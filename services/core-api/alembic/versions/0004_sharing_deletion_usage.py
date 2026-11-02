"""Phase 3: restricted documents + sharing grants (in RLS), deletion jobs, usage ledger + quotas.

Revision ID: 0004
"""

from alembic import op

revision = "0004"
down_revision = "0003"

# Statements separated by a line containing only "--;" (bodies contain semicolons).
# *_definer policies: SECURITY DEFINER helpers run as documind_owner, which FORCE RLS
# would otherwise blind. The owner role is never used by the running app.
UPGRADE = r"""
ALTER TABLE documents ADD COLUMN visibility text NOT NULL DEFAULT 'workspace'
  CHECK (visibility IN ('workspace','restricted'))
--;
CREATE TABLE sharing_grants (
  tenant_id uuid NOT NULL,
  document_id uuid NOT NULL,
  grantee_user_id uuid NOT NULL REFERENCES users(id),
  granted_by uuid NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz,
  FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id),
  FOREIGN KEY (tenant_id, grantee_user_id) REFERENCES memberships (tenant_id, user_id)
)
--;
CREATE UNIQUE INDEX uq_active_grant ON sharing_grants (document_id, grantee_user_id)
  WHERE revoked_at IS NULL
--;
CREATE FUNCTION app_is_tenant_admin(p_tenant uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = documind AS
$$ SELECT EXISTS (SELECT 1 FROM memberships WHERE tenant_id = p_tenant
                   AND user_id = app_user_id() AND role = 'tenant_admin') $$
--;
CREATE FUNCTION app_has_grant(p_document uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = documind AS
$$ SELECT EXISTS (SELECT 1 FROM sharing_grants WHERE document_id = p_document
                   AND grantee_user_id = app_user_id() AND revoked_at IS NULL) $$
--;
CREATE FUNCTION resolve_member_by_email(p_tenant uuid, p_email text) RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = documind AS
$$ SELECT u.id FROM users u JOIN memberships m ON m.user_id = u.id
   WHERE m.tenant_id = p_tenant AND lower(u.email) = lower(p_email)
     AND p_tenant IN (SELECT app_tenant_ids()) $$
--;
DROP POLICY documents_member ON documents
--;
CREATE POLICY documents_visible ON documents TO documind_app
  USING (tenant_id IN (SELECT app_tenant_ids()) AND (
    visibility = 'workspace' OR created_by = app_user_id()
    OR app_is_tenant_admin(tenant_id) OR app_has_grant(id)))
  WITH CHECK (tenant_id IN (SELECT app_tenant_ids()) AND (
    visibility = 'workspace' OR created_by = app_user_id() OR app_is_tenant_admin(tenant_id)))
--;
DROP POLICY chunks_member ON document_chunks
--;
CREATE POLICY chunks_visible ON document_chunks TO documind_app
  USING (tenant_id IN (SELECT app_tenant_ids())
         AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_chunks.document_id))
--;
ALTER TABLE sharing_grants ENABLE ROW LEVEL SECURITY
--;
ALTER TABLE sharing_grants FORCE ROW LEVEL SECURITY
--;
CREATE POLICY grants_visible ON sharing_grants TO documind_app
  USING (tenant_id IN (SELECT app_tenant_ids()) AND (grantee_user_id = app_user_id()
         OR granted_by = app_user_id() OR app_is_tenant_admin(tenant_id)))
  WITH CHECK (tenant_id IN (SELECT app_tenant_ids()) AND granted_by = app_user_id())
--;
CREATE TABLE tenant_quotas (
  tenant_id uuid PRIMARY KEY REFERENCES tenants(id),
  monthly_chat_tokens bigint NOT NULL,
  storage_bytes bigint NOT NULL
)
--;
CREATE TABLE usage_ledger (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  user_id uuid,
  kind text NOT NULL CHECK (kind IN ('chat_tokens','storage_bytes')),
  quantity bigint NOT NULL,
  ref_id uuid,
  created_at timestamptz NOT NULL DEFAULT now()
)
--;
CREATE INDEX idx_usage_tenant_kind_time ON usage_ledger (tenant_id, kind, created_at)
--;
ALTER TABLE tenant_quotas ENABLE ROW LEVEL SECURITY
--;
ALTER TABLE tenant_quotas FORCE ROW LEVEL SECURITY
--;
ALTER TABLE usage_ledger ENABLE ROW LEVEL SECURITY
--;
ALTER TABLE usage_ledger FORCE ROW LEVEL SECURITY
--;
CREATE POLICY quotas_member ON tenant_quotas TO documind_app
  USING (tenant_id IN (SELECT app_tenant_ids()))
--;
CREATE POLICY usage_member ON usage_ledger TO documind_app
  USING (tenant_id IN (SELECT app_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT app_tenant_ids()))
--;
CREATE POLICY usage_worker ON usage_ledger TO documind_worker
  USING (tenant_id = app_job_tenant_id()) WITH CHECK (tenant_id = app_job_tenant_id())
--;
GRANT EXECUTE ON FUNCTION app_is_tenant_admin(uuid), app_has_grant(uuid),
  resolve_member_by_email(uuid, text) TO documind_app
--;
GRANT EXECUTE ON FUNCTION app_is_tenant_admin(uuid), app_has_grant(uuid) TO documind_worker
--;
GRANT SELECT, INSERT, UPDATE ON sharing_grants TO documind_app
--;
GRANT SELECT ON tenant_quotas TO documind_app
--;
GRANT SELECT, INSERT ON usage_ledger TO documind_app
--;
GRANT SELECT, INSERT ON usage_ledger TO documind_worker
--;
CREATE POLICY grants_definer ON sharing_grants FOR SELECT TO documind_owner USING (true)
--;
CREATE POLICY memberships_definer ON memberships FOR SELECT TO documind_owner USING (true)
"""

DOWNGRADE = r"""
DROP POLICY memberships_definer ON memberships
--;
DROP TABLE usage_ledger, tenant_quotas
--;
DROP POLICY chunks_visible ON document_chunks
--;
CREATE POLICY chunks_member ON document_chunks TO documind_app
  USING (tenant_id IN (SELECT app_tenant_ids()))
--;
DROP POLICY documents_visible ON documents
--;
CREATE POLICY documents_member ON documents
  USING (tenant_id IN (SELECT app_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT app_tenant_ids()))
--;
DROP TABLE sharing_grants
--;
DROP FUNCTION resolve_member_by_email(uuid, text), app_has_grant(uuid), app_is_tenant_admin(uuid)
--;
ALTER TABLE documents DROP COLUMN visibility
"""


def _split(sql: str) -> list[str]:
    return [s.strip() for s in sql.split("\n--;\n") if s.strip()]


def upgrade() -> None:
    for stmt in _split(UPGRADE):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _split(DOWNGRADE):
        op.execute(stmt)
