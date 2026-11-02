"""Phase 2: redacted pages, chunks (FTS + pgvector), chat with validated citations.

Revision ID: 0003
"""

from alembic import op

revision = "0003"
down_revision = "0002"

UPGRADE = r"""
-- Raw extracted text may hold PII: only the worker may read it from now on.
REVOKE SELECT ON document_pages FROM documind_app;
ALTER TABLE document_pages ADD COLUMN redacted_text text;
ALTER TABLE documents ADD COLUMN redaction_counts jsonb;

CREATE TABLE document_chunks (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  document_id uuid NOT NULL,
  document_version integer NOT NULL,
  page_no integer NOT NULL,
  ordinal integer NOT NULL,
  char_start integer NOT NULL,
  char_end integer NOT NULL CHECK (char_end > char_start),
  text text NOT NULL,
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  embedding vector(256),
  embedding_model text,
  FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id),
  UNIQUE (document_id, document_version, ordinal)
);
CREATE INDEX idx_chunks_tsv ON document_chunks USING gin (tsv);
CREATE INDEX idx_chunks_embedding ON document_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_chunks_scope ON document_chunks (tenant_id, workspace_id);

CREATE TABLE chat_conversations (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  workspace_id uuid NOT NULL,
  user_id uuid NOT NULL REFERENCES users(id),
  title text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, workspace_id) REFERENCES workspaces (tenant_id, id)
);

CREATE TABLE chat_turns (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  conversation_id uuid NOT NULL REFERENCES chat_conversations(id),
  user_id uuid NOT NULL REFERENCES users(id),
  question text NOT NULL,
  answer text,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','streaming','completed','failed','canceled')),
  grounding text,
  failure_reason text,
  provider text,
  prompt_tokens integer,
  completion_tokens integer,
  cancel_requested boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);
CREATE INDEX idx_turns_conversation ON chat_turns (conversation_id, created_at);

CREATE TABLE chat_citations (
  turn_id uuid NOT NULL REFERENCES chat_turns(id),
  tenant_id uuid NOT NULL,
  user_id uuid NOT NULL,
  label text NOT NULL,
  ordinal integer NOT NULL,
  chunk_id uuid NOT NULL,
  document_id uuid NOT NULL,
  document_version integer NOT NULL,
  filename text NOT NULL,
  page_no integer NOT NULL,
  char_start integer NOT NULL,
  char_end integer NOT NULL,
  quote_preview text NOT NULL,
  PRIMARY KEY (turn_id, label)
);

ALTER TABLE document_chunks    ENABLE ROW LEVEL SECURITY; ALTER TABLE document_chunks    FORCE ROW LEVEL SECURITY;
ALTER TABLE chat_conversations ENABLE ROW LEVEL SECURITY; ALTER TABLE chat_conversations FORCE ROW LEVEL SECURITY;
ALTER TABLE chat_turns         ENABLE ROW LEVEL SECURITY; ALTER TABLE chat_turns         FORCE ROW LEVEL SECURITY;
ALTER TABLE chat_citations     ENABLE ROW LEVEL SECURITY; ALTER TABLE chat_citations     FORCE ROW LEVEL SECURITY;

CREATE POLICY chunks_member ON document_chunks TO documind_app
  USING (tenant_id IN (SELECT app_tenant_ids()));
CREATE POLICY chunks_worker ON document_chunks TO documind_worker
  USING (tenant_id = app_job_tenant_id()) WITH CHECK (tenant_id = app_job_tenant_id());

-- Chat history is private to its author (and their tenant membership).
CREATE POLICY conv_owner ON chat_conversations TO documind_app
  USING (tenant_id IN (SELECT app_tenant_ids()) AND user_id = app_user_id())
  WITH CHECK (tenant_id IN (SELECT app_tenant_ids()) AND user_id = app_user_id());
CREATE POLICY turns_owner ON chat_turns TO documind_app
  USING (tenant_id IN (SELECT app_tenant_ids()) AND user_id = app_user_id())
  WITH CHECK (tenant_id IN (SELECT app_tenant_ids()) AND user_id = app_user_id());
CREATE POLICY citations_owner ON chat_citations TO documind_app
  USING (tenant_id IN (SELECT app_tenant_ids()) AND user_id = app_user_id())
  WITH CHECK (tenant_id IN (SELECT app_tenant_ids()) AND user_id = app_user_id());

GRANT SELECT ON document_chunks TO documind_app;
GRANT SELECT, INSERT ON chat_conversations TO documind_app;
GRANT SELECT, INSERT, UPDATE ON chat_turns TO documind_app;
GRANT SELECT, INSERT ON chat_citations TO documind_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON document_chunks TO documind_worker;
GRANT UPDATE ON document_pages TO documind_worker;
"""

DOWNGRADE = r"""
DROP TABLE chat_citations, chat_turns, chat_conversations, document_chunks;
ALTER TABLE documents DROP COLUMN redaction_counts;
ALTER TABLE document_pages DROP COLUMN redacted_text;
GRANT SELECT ON document_pages TO documind_app;
"""


def _split(sql: str) -> list[str]:
    # One statement per asyncpg call. No $$ bodies or ';' in comments in this file.
    return [s.strip() for s in sql.split(";") if s.strip()
            and not all(ln.strip().startswith("--") for ln in s.strip().splitlines())]


def upgrade() -> None:
    for stmt in _split(UPGRADE):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _split(DOWNGRADE):
        op.execute(stmt)
