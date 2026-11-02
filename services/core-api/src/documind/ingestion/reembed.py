"""Backfill vectors for the active embedding model on already-indexed chunks.

Run after switching EMBEDDING_PROVIDER: python -m documind.ingestion.reembed
Runs as the worker role, one tenant at a time (tenants discovered from the jobs table,
which the worker may read across tenants). Idempotent: only fills missing vectors.
"""

import asyncio
import logging

from sqlalchemy import text

from documind.common.db import worker_unit_of_work
from documind.common.telemetry import setup_logging
from documind.retrieval.domain import to_pgvector
from documind.retrieval.embeddings import COLUMN_BY_DIM, get_embedder

log = logging.getLogger("documind.reembed")
BATCH = 64


async def reembed() -> int:
    embedder = get_embedder()
    col = COLUMN_BY_DIM[embedder.dim]
    async with worker_unit_of_work(None) as s:
        tenants = [r[0] for r in await s.execute(text("SELECT DISTINCT tenant_id FROM jobs"))]
    total = 0
    for tenant in tenants:
        while True:
            async with worker_unit_of_work(tenant) as s:
                rows = (await s.execute(text(
                    f"SELECT c.id, c.text FROM document_chunks c JOIN documents d "
                    f"ON d.id = c.document_id AND d.version = c.document_version "
                    f"WHERE d.status = 'ready' AND (c.{col} IS NULL OR c.embedding_model <> :m) "
                    f"LIMIT :n"), {"m": embedder.name, "n": BATCH})).all()
            if not rows:
                break
            vectors = await embedder.embed_documents([r.text for r in rows])
            async with worker_unit_of_work(tenant) as s:
                await s.execute(text(
                    f"UPDATE document_chunks SET {col} = CAST(:e AS vector), "
                    "embedding_model = :m WHERE id = :id"),
                    [{"e": to_pgvector(v), "m": embedder.name, "id": r.id}
                     for r, v in zip(rows, vectors, strict=True)])
            total += len(rows)
            log.info("re-embedded %s chunks (tenant %s)", total, tenant)
    return total


if __name__ == "__main__":
    setup_logging("documind-reembed")
    print(f"re-embedded {asyncio.run(reembed())} chunks with {get_embedder().name}")
