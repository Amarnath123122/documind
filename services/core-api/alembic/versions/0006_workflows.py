"""Phase 6: report workflows with human approval (four-eyes).

Revision ID: 0006
"""

from alembic import op

revision = "0006"
down_revision = "0005"

UPGRADE = r"""
CREATE TABLE workflow_runs (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  workspace_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('compare_documents')),
  title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  status text NOT NULL CHECK (status IN ('pending_approval','approved','rejected')),
  created_by uuid NOT NULL REFERENCES users(id),
  draft text NOT NULL,
  sources jsonb NOT NULL,
  decided_by uuid REFERENCES users(id),
  decision_note text,
  decided_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces (tenant_id, id),
  -- Four-eyes in the schema itself, not only in application code.
  CHECK (decided_by IS NULL OR decided_by <> created_by)
)
--;
CREATE INDEX idx_workflows_ws ON workflow_runs (tenant_id, workspace_id, created_at DESC)
--;
ALTER TABLE workflow_runs ENABLE ROW LEVEL SECURITY
--;
ALTER TABLE workflow_runs FORCE ROW LEVEL SECURITY
--;
CREATE POLICY workflows_visible ON workflow_runs TO documind_app
  USING (tenant_id IN (SELECT app_tenant_ids())
         AND (created_by = app_user_id() OR app_is_tenant_admin(tenant_id)))
  WITH CHECK (tenant_id IN (SELECT app_tenant_ids()))
--;
GRANT SELECT, INSERT, UPDATE ON workflow_runs TO documind_app
"""

DOWNGRADE = r"""
DROP TABLE workflow_runs
"""


def _split(sql: str) -> list[str]:
    return [s.strip() for s in sql.split("\n--;\n") if s.strip()]


def upgrade() -> None:
    for stmt in _split(UPGRADE):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _split(DOWNGRADE):
        op.execute(stmt)
