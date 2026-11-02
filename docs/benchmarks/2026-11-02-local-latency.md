# Local latency baseline: 2026-11-02

**What this is:** a single-machine baseline that catches regressions. It is **not** a capacity or SLO claim.

- **Machine:** Windows 11 laptop, Docker Desktop (WSL2). All containers on one host. Tracing on (Jaeger profile).
- **Data:** about 111 chunks across two tenants (synthetic). Mock LLM. `hash-v1-256` embeddings.
- **Command:** `services/core-api/.venv/Scripts/python tools/bench/latency.py 200 10`

| Endpoint | Requests | Concurrency | p50 | p95 | max |
|---|---|---|---|---|---|
| `GET /workspaces/{id}/documents` | 200 | 10 | 55.8 ms | 87.4 ms | 142.7 ms |
| `GET /workspaces/{id}/search` (hybrid) | 200 | 10 | 52.2 ms | 129.7 ms | 187.4 ms |
| Chat turn (create + full SSE stream, mock LLM) | 50 | 5 | 149.2 ms | 183.2 ms | 185.3 ms |

## How to read this
- Each request does 2–5 short transactions (principal load, RLS context, query), each on a fresh `set_config`. This is the cost of per-request authoritative authorization with no cache.
- Chat time is dominated by retrieval plus about 6 small transactions. A real LLM adds its own time-to-first-token on top; measure that separately per provider.
- Before setting SLOs, re-run on production-like infrastructure with production-sized data: per-tenant chunk counts, HNSW recall versus filters (ADR-0006), and connection-pool sizing.
