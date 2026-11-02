"""Phase 1: upload sessions, durable jobs, extracted pages, worker role policies.

Revision ID: 0002
"""

from alembic import op

revision = "0002"
down_revision = "0001"

UPGRADE = r"""
-- Tenant set by the worker from the claimed job row (never from a client).
CREATE FUNCTION app_job_tenant_id() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$;

ALTER TABLE documents
  ADD COLUMN mime text,
  ADD COLUMN size_bytes bigint CHECK (size_bytes >= 0),
  ADD COLUMN blob_etag text,
  ADD COLUMN sha256 text,
  ADD COLUMN page_count integer,
  ADD COLUMN failure_reason text,
  ADD CONSTRAINT documents_tenant_id_key UNIQUE (tenant_id, id);

CREATE TABLE upload_sessions (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  document_id uuid NOT NULL UNIQUE,
  blob_path text NOT NULL,
  declared_size bigint NOT NULL CHECK (declared_size > 0),
  declared_mime text NOT NULL,
  status text NOT NULL CHECK (status IN ('pending','completed','failed')),
  idempotency_key text,
  created_by uuid NOT NULL REFERENCES users(id),
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id),
  UNIQUE (tenant_id, created_by, idempotency_key)
);

CREATE TABLE jobs (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  kind text NOT NULL,
  document_id uuid NOT NULL,
  document_version integer NOT NULL,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','succeeded','failed','dead','abandoned')),
  attempts integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 5,
  run_after timestamptz NOT NULL DEFAULT now(),
  lease_until timestamptz,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id),
  -- Idempotent enqueue: one ingest job per document version.
  UNIQUE (kind, document_id, document_version)
);
CREATE INDEX idx_jobs_claimable ON jobs (run_after) WHERE status IN ('queued','running');

CREATE TABLE document_pages (
  tenant_id uuid NOT NULL,
  document_id uuid NOT NULL,
  page_no integer NOT NULL CHECK (page_no >= 1),
  text text NOT NULL,
  PRIMARY KEY (document_id, page_no),
  FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id)
);

ALTER TABLE upload_sessions ENABLE ROW LEVEL SECURITY; ALTER TABLE upload_sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE jobs            ENABLE ROW LEVEL SECURITY; ALTER TABLE jobs            FORCE ROW LEVEL SECURITY;
ALTER TABLE document_pages  ENABLE ROW LEVEL SECURITY; ALTER TABLE document_pages  FORCE ROW LEVEL SECURITY;

-- Only the user who started an upload can see/complete it.
CREATE POLICY uploads_owner ON upload_sessions TO documind_app
  USING (tenant_id IN (SELECT app_tenant_ids()) AND created_by = app_user_id())
  WITH CHECK (tenant_id IN (SELECT app_tenant_ids()) AND created_by = app_user_id());

CREATE POLICY jobs_member ON jobs TO documind_app
  USING (tenant_id IN (SELECT app_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT app_tenant_ids()));
-- Claiming is cross-tenant by design: job rows hold ids and status only, never content.
-- (No semicolons in comments here: _split() is a simple splitter.)
CREATE POLICY jobs_worker ON jobs TO documind_worker USING (true) WITH CHECK (true);

CREATE POLICY documents_worker ON documents TO documind_worker
  USING (tenant_id = app_job_tenant_id()) WITH CHECK (tenant_id = app_job_tenant_id());

CREATE POLICY pages_member ON document_pages TO documind_app
  USING (tenant_id IN (SELECT app_tenant_ids()));
CREATE POLICY pages_worker ON document_pages TO documind_worker
  USING (tenant_id = app_job_tenant_id()) WITH CHECK (tenant_id = app_job_tenant_id());

GRANT SELECT, INSERT, UPDATE ON upload_sessions TO documind_app;
GRANT SELECT, INSERT ON jobs TO documind_app;
GRANT SELECT ON document_pages TO documind_app;

-- Phase 0 policies apply to PUBLIC, so the worker must be able to evaluate them.
-- They yield nothing for the worker: it never sets app.user_id.
GRANT EXECUTE ON FUNCTION app_job_tenant_id(), app_user_id(), app_tenant_ids() TO documind_worker;
GRANT SELECT, UPDATE ON jobs, documents TO documind_worker;
GRANT SELECT, INSERT, DELETE ON document_pages TO documind_worker;
"""

DOWNGRADE = r"""
DROP TABLE document_pages, jobs, upload_sessions;
ALTER TABLE documents DROP CONSTRAINT documents_tenant_id_key,
  DROP COLUMN mime, DROP COLUMN size_bytes, DROP COLUMN blob_etag, DROP COLUMN sha256,
  DROP COLUMN page_count, DROP COLUMN failure_reason;
DROP POLICY IF EXISTS documents_worker ON documents;
DROP FUNCTION app_job_tenant_id();
"""


def _split(sql: str) -> list[str]:
    # asyncpg runs one statement per call; split on ';' outside $$-quoted bodies.
    out, buf, in_dollar, i = [], [], False, 0
    while i < len(sql):
        if sql.startswith("$$", i):
            in_dollar = not in_dollar
            buf.append("$$")
            i += 2
            continue
        ch = sql[i]
        if ch == ";" and not in_dollar:
            stmt = "".join(buf).strip()
            if stmt and not all(ln.strip().startswith("--") for ln in stmt.splitlines()):
                out.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    return out


def upgrade() -> None:
    for stmt in _split(UPGRADE):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _split(DOWNGRADE):
        op.execute(stmt)
