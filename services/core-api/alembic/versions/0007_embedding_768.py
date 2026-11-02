"""Phase 7: second vector column for 768-dim models (e.g. nomic-embed-text).

Revision ID: 0007
"""

from alembic import op

revision = "0007"
down_revision = "0006"


def upgrade() -> None:
    op.execute("ALTER TABLE document_chunks ADD COLUMN embedding_768 vector(768)")
    op.execute("CREATE INDEX idx_chunks_embedding_768 ON document_chunks "
               "USING hnsw (embedding_768 vector_cosine_ops)")


def downgrade() -> None:
    op.execute("DROP INDEX idx_chunks_embedding_768")
    op.execute("ALTER TABLE document_chunks DROP COLUMN embedding_768")
