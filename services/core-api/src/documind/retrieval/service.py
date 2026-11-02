"""Permission-filtered hybrid retrieval (PostgreSQL FTS + pgvector, fused with RRF).

Security ordering (handbook 24.4): scope filters are inside both SQL queries, *before*
ranking and LIMIT. Only chunks whose document is READY at the same version are eligible,
so deleting/reprocessing documents vanish from search immediately.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from documind.common.config import get_settings
from documind.retrieval.domain import hash_embed, rrf, to_pgvector

_SCOPE = (
    "FROM document_chunks c JOIN documents d "
    "  ON d.id = c.document_id AND d.version = c.document_version "
    "WHERE d.status = 'ready' AND c.tenant_id = ANY(:tenants) AND c.workspace_id = ANY(:ws) "
)
# Tie-break on content position, never on random ids, so identical corpora rank identically.
_STABLE = "d.filename, c.page_no, c.ordinal, c.id"
# OR the query terms (websearch_to_tsquery ANDs them, which is too strict for questions).
_TSQ = "replace(websearch_to_tsquery('english', :q)::text, '&', '|')::tsquery"


@dataclass(frozen=True)
class Hit:
    chunk_id: UUID
    document_id: UUID
    document_version: int
    filename: str
    workspace_id: UUID
    page_no: int
    char_start: int
    char_end: int
    text: str


async def hybrid_search(s: AsyncSession, *, tenant_ids: list[UUID], workspace_ids: list[UUID],
                        query: str, top_k: int | None = None) -> list[Hit]:
    cfg = get_settings()
    if not tenant_ids or not workspace_ids or not query.strip():
        return []
    scope = {"tenants": tenant_ids, "ws": workspace_ids, "n": cfg.retrieval_candidates}

    keyword = [r[0] for r in await s.execute(text(
        f"SELECT c.id {_SCOPE} AND {_TSQ}::text <> '' AND c.tsv @@ {_TSQ} "
        f"ORDER BY ts_rank_cd(c.tsv, {_TSQ}) DESC, {_STABLE} LIMIT :n"),
        {**scope, "q": query})]
    vector = [r[0] for r in await s.execute(text(
        f"SELECT c.id {_SCOPE} AND c.embedding IS NOT NULL "
        f"ORDER BY c.embedding <=> CAST(:e AS vector), {_STABLE} LIMIT :n"),
        {**scope, "e": to_pgvector(hash_embed(query))})]

    ranked = rrf([str(i) for i in keyword], [str(i) for i in vector])[: top_k or cfg.retrieval_top_k]
    if not ranked:
        return []
    rows = {str(r.chunk_id): r for r in await s.execute(text(
        "SELECT c.id AS chunk_id, c.document_id, c.document_version, d.filename, c.workspace_id, "
        f"c.page_no, c.char_start, c.char_end, c.text {_SCOPE} AND c.id = ANY(:ids)"),
        {**scope, "ids": [UUID(i) for i in ranked]})}
    return [Hit(**rows[i]._asdict()) for i in ranked if i in rows]
