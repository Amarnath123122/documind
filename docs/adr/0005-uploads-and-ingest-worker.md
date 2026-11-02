# ADR-0005: Direct-to-blob uploads, quarantine, and the ingest worker

**Status:** Accepted (Phase 1)

## Upload flow
1. `POST /v1/workspaces/{ws}/uploads` checks membership, `document:create`, the type allowlist (PDF/TXT/MD/DOCX) and the size cap (25 MB). It creates the document in `uploading` and returns a SAS that is **write-only, for one server-chosen blob path, and valid for 15 minutes**. An `Idempotency-Key` makes retries return the same document.
2. The browser PUTs bytes directly to `quarantine/`, so the API never streams file bodies. Locally this goes through the web origin's proxy, so the upload is same-origin and needs no CORS.
3. `POST /v1/uploads/{id}/complete` checks the **actual stored bytes**: size equals the declared size, and the leading bytes match the declared type. On success it moves the document to `quarantined`, records the blob **ETag**, and enqueues a job **in the same transaction**. Only the user who started the upload can complete it (enforced by RLS), and repeated calls are idempotent.

## Worker
- Separate process (`python -m documind.ingestion.worker`) using its own DB role, `documind_worker`. It can claim any job row, but job rows hold only ids and status. For document data it sets `app.tenant_id` **from the claimed job row** and RLS limits it to that tenant. It has no access to identity tables and cannot write audit events.
- Claims with `FOR UPDATE SKIP LOCKED` plus a 120 s lease. A crashed worker's job is reclaimed once the lease expires.
- Every state move is a compare-and-set (`WHERE status = expected AND version = v`). If a document is moved to `deleting` mid-flight, the job is **abandoned** rather than marking the document ready.
- Scan reads the quarantine blob **with `If-Match: <etag>`**, so bytes swapped after verification (the SAS is still live) fail permanently with `content_changed_after_verification`.
- Error classes: `PermanentError` (malware, encrypted or corrupt file, zip bomb, too many pages) fails immediately with a user-visible reason. Anything else is transient: retried with exponential backoff and jitter, then dead-lettered (`status = 'dead'`) after `max_attempts`, and the document is marked failed. Logged errors contain only the exception class name, never content or URLs.

## Local vs production
| Concern | Local | Production (before launch) |
|---|---|---|
| SAS signing | Azurite account key | **User-delegation SAS** via managed identity, no account keys |
| Malware scan | EICAR-only test scanner | Defender for Storage or ClamAV behind the `Scanner` port |
| Queue | Postgres `jobs` table | Same, until measured load says otherwise (ADR-0002) |

`Settings` refuses to start with Azurite storage when `ENV=production`.

## Phase 2 hook
Redacting, chunking, embedding and indexing are currently **pass-through** stages in the state machine, so documents reach `ready` with extracted pages only. Phase 2 replaces them; nothing searchable exists yet.
