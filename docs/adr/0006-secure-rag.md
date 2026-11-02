# ADR-0006: Secure RAG: redaction, hybrid retrieval, validated citations

**Status:** Accepted (Phase 2)

## Pipeline (worker, after extraction)
`redacting → chunking → embedding → indexing → ready`. Each stage writes and moves the document state (compare-and-set) in **one transaction**. If the document left the expected state (e.g. `deleting`), the stage's writes roll back.

- **Redaction:** deterministic recognizers (email, phone, Luhn-checked cards, PAN, Aadhaar, SSN), applied as permanent `[TYPE_REDACTED]` markers. Only per-type *counts* are kept. Raw page text is now readable **only by the worker role** (`REVOKE SELECT ... FROM documind_app`). Next step: Presidio/NER recognizers behind the same `redact()` boundary, evaluated for false positives and negatives.
- **Chunks** never span pages, and their offsets index into the redacted page text, so every citation has one exact page. (Defaults: 800 characters per chunk, 120 overlapping. To be tuned against a golden set.)
- **Embeddings:** `hash-v1-256`, a deterministic feature-hashing model that runs offline and costs nothing. It captures **lexical overlap only**: it proves the wiring, not semantic quality. The model name is stored per chunk. Switching to a real model (e.g. Ollama `nomic-embed-text`, Azure OpenAI) means a new column/dimension plus a re-embedding job, never mixing vectors from different models.
- **Go-live switch:** retrieval only serves chunks whose document is `ready` *at the same version*, so the final status flip makes the document searchable atomically.

## Retrieval
Postgres full-text search (terms OR'ed) and pgvector cosine search, fused with **reciprocal-rank fusion** (k=60). Tenant, workspace and `ready` filters sit **inside both queries, before LIMIT**. RLS on `document_chunks` enforces tenant scope regardless.
Known limit: HNSW with post-filtering can lose recall when one tenant is a small fraction of a large index. Benchmark this, and add per-tenant partial indexes or `hnsw.iterative_scan` before scaling.

## Chat
- `POST /v1/chat/turns` → `GET .../events` (SSE: `started`, `delta`, `citation`, `usage`, `completed` | `failed` | `canceled`, with monotonic ids) → `POST .../cancel`.
- Order: fresh principal → permission check → retrieve under RLS → **re-check** each source is still `ready` at the same version right before the model call → fenced, labelled evidence (`E1..En`), declared as untrusted data → stream → **server-side citation validation**. Labels the server didn't issue (e.g. `[E99]`) are stripped and never stored.
- No evidence → fixed "couldn't find" answer, and **the model is not called**.
- `delta` text is provisional; the `completed` event carries the final, validated answer.
- Revisiting history re-validates citations: if a source is deleted or revoked since, it shows `available: false` and the quote is withheld.
- Chat history is private to its author (RLS on `user_id`).
- Completed turns replay from the database, so reconnecting never re-runs (or re-bills) the model. Generation is never retried automatically.

## Providers
`MockChatProvider` (deterministic, extractive, used in CI) and `OllamaChatProvider` (`LLM_PROVIDER=ollama`). The Ollama adapter is **not exercised by automated tests**. Azure OpenAI is added at deployment behind the same interface. Prompt injection is mitigated by data-flow controls (no tools, evidence fencing, citation validation), not by the prompt alone.
