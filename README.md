# DocuMind

Secure, multi-tenant AI knowledge platform, built as a modular monolith.
Status: **Phase 7 (real local models, measured) complete locally.** Not yet deployed: see `docs/runbooks/deploy.md`.

## What exists

| Area | Where | What it proves |
|---|---|---|
| Postgres roles + RLS | `infra/compose/initdb`, `services/core-api/alembic/versions` | API and worker roles cannot bypass tenant isolation |
| Identity + RBAC | `services/core-api/src/documind/identity` | Roles → permissions; fail-closed checks |
| Documents + uploads | `services/core-api/src/documind/documents` | Write-only SAS, byte verification, quarantine + enqueue in one tx |
| Ingest worker | `services/core-api/src/documind/ingestion` | Lease/SKIP LOCKED, CAS states, scan, extract, redact, chunk, embed, retry, dead letter |
| Privacy | `services/core-api/src/documind/privacy` | Only redacted text is indexed or sent to a model |
| Retrieval | `services/core-api/src/documind/retrieval` | FTS + pgvector hybrid search, filtered before ranking |
| Chat | `services/core-api/src/documind/chat` | SSE streaming, cancel, server-validated citations, private history |
| Sharing + deletion | `documents/service.py`, migration `0004` | Restricted docs enforced in RLS, instant revocation, deny-first delete |
| Usage | `services/core-api/src/documind/usage` | Ledger + per-tenant quotas, 429 when exhausted |
| Observability | `common/telemetry.py` | JSON logs + secret scrubbing, OpenTelemetry traces/metrics (opt-in) |
| Security docs | `docs/security`, `docs/runbooks`, `docs/benchmarks` | STRIDE threat model ↔ tests, job runbook, measured latency |
| Auth | `identity/session.py`, `identity/oidc.py` | OIDC code + PKCE (tested with a mock IdP), signed session, CSRF; dev login blocked in prod |
| Evals | `tools/evals/golden.json`, `tests/integration/test_eval_golden.py` | Golden-set quality gate (hit@1, MRR, citations, abstain) in CI |
| Workflows | `services/core-api/src/documind/workflows` | Cited report drafts, four-eyes approval (code + DB constraint), live source checks |
| Real models | `retrieval/embeddings.py`, `chat/providers.py` | Ollama nomic-embed-text + qwen2.5, compared on the golden set (ADR-0011) |
| Production web | `apps/web/Dockerfile`, `apps/web/deploy` | nginx, same-origin proxy, SSE, strict CSP/HSTS on every response |
| Azure IaC | `infra/terraform` | Container Apps, Postgres HA, keyless storage, Key Vault, managed identities (validated, not applied) |
| React app | `apps/web` | Sign in, upload, watch processing, chat with cited answers |
| Decisions | `docs/adr` | 0001 monolith · 0002 no Kafka · 0003 RLS · 0004 session · 0005 uploads/worker · 0006 secure RAG · 0007 sharing/deletion/quotas · 0008 observability · 0009 OIDC + Azure · 0010 evals + workflows · 0011 real models |

## Run (Windows PowerShell)

Requirements: Docker Desktop, Python 3.12+. The web app runs on Node 22 inside Docker.

```powershell
.\scripts\dev.ps1 up      # db, azurite → migrate + seed → api, worker, web
.\scripts\dev.ps1 test    # 165 tests (pauses the worker container while running)
.\scripts\dev.ps1 observe # same as up + Jaeger traces at http://localhost:16686
.\scripts\dev.ps1 eval    # golden-set quality report -> docs/benchmarks/golden-eval-latest.md
.\scripts\dev.ps1 down    # stop (keep data)   |  reset = delete db + blob volumes
```

- Web: http://localhost:5173. Sign in as `alice@alpha.test` (Alpha admin), `dave@alpha.test` (Alpha editor), `carol@alpha.test` (Alpha viewer) or `bob@beta.test` (Beta editor).
- API docs: http://localhost:8000/docs

## Verify by hand

1. Sign in as Alice and upload a PDF. The status moves `quarantined → … → ready`, and the page count appears.
2. Upload a `.txt` file renamed to `.pdf`. It is rejected: **content type mismatch**.
3. Upload a text file containing the [EICAR test string](https://www.eicar.org/download-anti-malware-testfile/). It ends **failed: malware detected**, with no retries.
4. Open **Chat** and ask “How many casual leave days?”. The answer streams in, with a citation to the file and page. Personal details (emails, phone numbers) show as `[EMAIL_REDACTED]` and so on.
5. As Alice, click **Restrict** on a document. Carol can no longer see it in the list, search or chat, until Alice shares it with `carol@alpha.test`.
6. **Delete** a document. It disappears from search at once, and cleanup removes its text, index and file within seconds.
7. Sign in as Carol (viewer). There is no upload control, and the API returns 403. Sign in as Bob. He sees only **Beta Legal**.

## Architecture rules (enforced by tests)

- Domain modules import no frameworks (`test_domain_has_no_framework_imports`).
- Runtime DB roles are not superuser and not BYPASSRLS (`test_app_role_is_not_privileged`).
- The API sets only `app.user_id`. The worker sets only `app.tenant_id`, taken from a claimed job row (ADR-0003, ADR-0005).
- Raw page text is readable only by the worker; chunks and prompts contain redacted text only.
- Citations the server didn't issue are dropped (`test_hallucinated_citation_never_reaches_client`).
- Revoking a grant hides the document on the very next request (`test_restricted_document_hidden_everywhere_until_granted`).
- A document in `deleting` is never moved back to `ready` (`test_deleting_document_is_never_revived`).

## Architecture

Full architecture with diagrams and evidence for every claim: [docs/architecture/README.md](docs/architecture/README.md).
