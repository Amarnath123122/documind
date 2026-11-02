# Runbook: ingestion and cleanup jobs

All SQL below runs as an operator with audited access, never as the app or worker role. Every replay is recorded in the incident ticket.

## Find stuck or failed work
```sql
SET search_path = documind;
-- Dead-lettered jobs (retries exhausted)
SELECT id, kind, tenant_id, document_id, attempts, last_error, updated_at
FROM jobs WHERE status = 'dead' ORDER BY updated_at DESC LIMIT 50;
-- Documents needing a human
SELECT id, tenant_id, status, failure_reason, updated_at
FROM documents WHERE status IN ('needs_attention') OR (status = 'failed'
  AND failure_reason = 'processing_failed_after_retries');
-- Leases that look stuck (worker crashed; they are reclaimed automatically after expiry)
SELECT id, kind, lease_until FROM jobs WHERE status = 'running' AND lease_until < now();
```
`last_error` holds only an exception class name, by design. Correlate it with worker logs using `job <id>` and the trace in Jaeger (`worker.job`, attribute `job.id`).

## Replay a dead **ingest** job (after fixing the cause)
Only while the document is still in a pipeline state at the same version:
```sql
BEGIN;
UPDATE documents SET status = 'quarantined', failure_reason = NULL, updated_at = now()
 WHERE id = :doc AND status = 'failed' AND failure_reason = 'processing_failed_after_retries';
UPDATE jobs SET status = 'queued', attempts = 0, run_after = now(), last_error = NULL
 WHERE id = :job AND status = 'dead' AND kind = 'ingest';
COMMIT;
```
The pipeline is idempotent: it resumes from `quarantined` (or from `originals` if the bytes were already moved).

## Retry a failed **cleanup** (`needs_attention`)
```sql
BEGIN;
UPDATE documents SET status = 'deleting', failure_reason = NULL WHERE id = :doc AND status = 'needs_attention';
UPDATE jobs SET status = 'queued', attempts = 0, run_after = now() WHERE id = :job AND kind = 'cleanup';
COMMIT;
```
The document stays invisible to users throughout. Never set `deleted` by hand: cleanup also refunds quota and removes every blob version.

## Never
- Delete rows from `jobs`, `audit_events` or `usage_ledger` to "clean up".
- Run the app with the owner or superuser role to work around RLS.
