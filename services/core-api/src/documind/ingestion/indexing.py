"""Phase 2 stages: redact -> chunk -> embed -> index.

Each stage does its writes AND its compare-and-set status move in one transaction, so a
document that left the expected state (e.g. deleting) rolls the stage's writes back.
Only redacted text ever reaches chunks, the search index, or a model.
"""

import asyncio
import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from documind.common.db import worker_unit_of_work
from documind.documents.domain import DocumentStatus as S
from documind.ingestion.domain import Abandoned
from documind.privacy.domain import redact
from documind.retrieval.domain import HASH_MODEL, chunk_pages, hash_embed, to_pgvector


async def _cas(s: AsyncSession, job, frm: S, to: S, extra_sql: str = "", **params) -> None:
    res = await s.execute(text(
        f"UPDATE documents SET status = :to, updated_at = now(){extra_sql} "
        "WHERE id = :id AND status = :frm AND version = :v"),
        {"id": job.document_id, "frm": frm.value, "to": to.value,
         "v": job.document_version, **params})
    if res.rowcount != 1:
        raise Abandoned(f"document left {frm}")  # exiting the block rolls back stage writes


async def redact_stage(job) -> None:
    async with worker_unit_of_work(job.tenant_id) as s:
        pages = (await s.execute(text(
            "SELECT page_no, text FROM document_pages WHERE document_id = :d ORDER BY page_no"),
            {"d": job.document_id})).all()
        totals: dict[str, int] = {}
        for p in pages:
            r = redact(p.text)
            for k, v in r.counts.items():
                totals[k] = totals.get(k, 0) + v
            await s.execute(text(
                "UPDATE document_pages SET redacted_text = :t WHERE document_id = :d "
                "AND page_no = :n"), {"t": r.text, "d": job.document_id, "n": p.page_no})
        await _cas(s, job, S.REDACTING, S.CHUNKING,
                   ", redaction_counts = CAST(:rc AS jsonb)", rc=json.dumps(totals))


async def chunk_stage(job) -> None:
    async with worker_unit_of_work(job.tenant_id) as s:
        ws = (await s.execute(text("SELECT workspace_id FROM documents WHERE id = :d"),
                              {"d": job.document_id})).scalar_one()
        pages = [r[0] for r in await s.execute(text(
            "SELECT redacted_text FROM document_pages WHERE document_id = :d ORDER BY page_no"),
            {"d": job.document_id})]
        if any(p is None for p in pages):
            raise RuntimeError("unredacted page reached chunking")  # never index raw text
        chunks = chunk_pages(pages)
        await s.execute(text(
            "DELETE FROM document_chunks WHERE document_id = :d AND document_version = :v"),
            {"d": job.document_id, "v": job.document_version})
        if chunks:
            await s.execute(text(
                "INSERT INTO document_chunks (id, tenant_id, workspace_id, document_id, "
                "document_version, page_no, ordinal, char_start, char_end, text) VALUES "
                "(:id, :t, :w, :d, :v, :p, :o, :cs, :ce, :x)"),
                [{"id": uuid.uuid4(), "t": job.tenant_id, "w": ws, "d": job.document_id,
                  "v": job.document_version, "p": c.page_no, "o": c.ordinal,
                  "cs": c.char_start, "ce": c.char_end, "x": c.text} for c in chunks])
        await _cas(s, job, S.CHUNKING, S.EMBEDDING)


async def embed_stage(job) -> None:
    async with worker_unit_of_work(job.tenant_id) as s:
        rows = (await s.execute(text(
            "SELECT id, text FROM document_chunks WHERE document_id = :d "
            "AND document_version = :v AND embedding IS NULL"),
            {"d": job.document_id, "v": job.document_version})).all()
        vectors = await asyncio.to_thread(lambda: [hash_embed(r.text) for r in rows])
        if rows:
            await s.execute(text(
                "UPDATE document_chunks SET embedding = CAST(:e AS vector), embedding_model = :m "
                "WHERE id = :id"),
                [{"e": to_pgvector(v), "m": HASH_MODEL, "id": r.id}
                 for r, v in zip(rows, vectors, strict=True)])
        await _cas(s, job, S.EMBEDDING, S.INDEXING)


async def index_stage(job) -> None:
    async with worker_unit_of_work(job.tenant_id) as s:
        missing = (await s.execute(text(
            "SELECT count(*) FROM document_chunks WHERE document_id = :d "
            "AND document_version = :v AND embedding IS NULL"),
            {"d": job.document_id, "v": job.document_version})).scalar_one()
        if missing:
            raise RuntimeError("chunks without embeddings at indexing")
        # Retrieval only serves chunks whose document is READY at the same version,
        # so this single status flip is the atomic "go live" switch.
        await _cas(s, job, S.INDEXING, S.READY)


STAGES = {S.REDACTING: redact_stage, S.CHUNKING: chunk_stage,
          S.EMBEDDING: embed_stage, S.INDEXING: index_stage}
ORDER = [S.REDACTING, S.CHUNKING, S.EMBEDDING, S.INDEXING]
