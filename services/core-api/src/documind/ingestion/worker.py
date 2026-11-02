"""Ingest worker: separate process, same codebase (ADR-0001/0005).

Delivery is at-least-once. Safety comes from:
- claiming with FOR UPDATE SKIP LOCKED + a lease (a crashed worker's job is reclaimed),
- compare-and-set status moves (a deleted/advanced document is never touched),
- idempotent stages (re-running a stage overwrites, never duplicates).

Run: python -m documind.ingestion.worker
"""

import asyncio
import hashlib
import logging
import signal
from dataclasses import dataclass
from uuid import UUID

from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
from sqlalchemy import text

from documind.common.config import get_settings
from documind.common.db import worker_unit_of_work
from documind.common.storage import ORIGINALS, QUARANTINE, BlobStorage, get_storage
from documind.common.telemetry import jobs_total, setup_logging, setup_tracing, tracer
from documind.documents.domain import DocumentStatus as S
from documind.ingestion.domain import Abandoned, PermanentError, backoff_s
from documind.ingestion.extract import extract_pages
from documind.ingestion.indexing import ORDER, STAGES
from documind.ingestion.scanner import EicarScanner, Scanner

log = logging.getLogger("documind.worker")
CLEANUP = "cleanup"

IN_PIPELINE = (S.QUARANTINED, S.SCANNING, S.EXTRACTING, S.REDACTING, S.CHUNKING,
               S.EMBEDDING, S.INDEXING)


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    tenant_id: UUID
    document_id: UUID
    document_version: int
    attempts: int
    max_attempts: int


async def claim() -> Job | None:
    lease = get_settings().worker_lease_s
    async with worker_unit_of_work(None) as s:
        r = (await s.execute(text(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = now(), "
            "  lease_until = now() + make_interval(secs => :lease) "
            "WHERE id = (SELECT id FROM jobs "
            "  WHERE ((status = 'queued' AND run_after <= now()) "
            "     OR (status = 'running' AND lease_until < now())) "
            "  ORDER BY run_after LIMIT 1 FOR UPDATE SKIP LOCKED) "
            "RETURNING id, kind, tenant_id, document_id, document_version, attempts, max_attempts"),
            {"lease": float(lease)})).one_or_none()
    return Job(*r) if r else None


async def _finish(job: Job, status: str, error: str | None = None,
                  retry_in_s: float | None = None) -> None:
    async with worker_unit_of_work(None) as s:
        await s.execute(text(
            "UPDATE jobs SET status = :st, last_error = :err, lease_until = NULL, "
            "  run_after = COALESCE(now() + make_interval(secs => CAST(:delay AS float8)), "
            "                       run_after), "
            "  updated_at = now() WHERE id = :id"),
            {"st": status, "err": error, "delay": retry_in_s, "id": job.id})


async def _cas(job: Job, frm: S, to: S, **fields) -> None:
    sets = "".join(f", {k} = :{k}" for k in fields)
    async with worker_unit_of_work(job.tenant_id) as s:
        res = await s.execute(text(
            f"UPDATE documents SET status = :to, updated_at = now(){sets} "
            "WHERE id = :id AND status = :frm AND version = :v"),
            {"id": job.document_id, "frm": frm.value, "to": to.value,
             "v": job.document_version, **fields})
    if res.rowcount != 1:
        raise Abandoned(f"document left {frm}")


async def _fail_document(job: Job, reason: str) -> None:
    # A failed cleanup must not look deleted: it needs a human (runbook: retry cleanup).
    target, states = ((S.NEEDS_ATTENTION, [S.DELETING]) if job.kind == CLEANUP
                      else (S.FAILED, list(IN_PIPELINE)))
    async with worker_unit_of_work(job.tenant_id) as s:
        await s.execute(text(
            "UPDATE documents SET status = :target, failure_reason = :r, updated_at = now() "
            "WHERE id = :id AND version = :v AND status = ANY(:states)"),
            {"target": target.value, "r": reason, "id": job.document_id,
             "v": job.document_version, "states": [st.value for st in states]})


async def run_ingest(job: Job, storage: BlobStorage, scanner: Scanner) -> None:
    async with worker_unit_of_work(job.tenant_id) as s:
        doc = (await s.execute(text(
            "SELECT status, version, mime, blob_etag FROM documents WHERE id = :id"),
            {"id": job.document_id})).one_or_none()
    if doc is None or doc.version != job.document_version:
        raise Abandoned("document missing or superseded")
    status = S(doc.status)
    if status == S.READY:
        return
    if status not in IN_PIPELINE:
        raise Abandoned(f"document is {status}")
    path = f"{job.tenant_id}/{job.document_id}/v{job.document_version}"

    if status == S.QUARANTINED:
        await _cas(job, S.QUARANTINED, S.SCANNING)
        status = S.SCANNING

    if status == S.SCANNING:
        try:
            data = await storage.read_all(QUARANTINE, path, etag=doc.blob_etag)
        except ResourceModifiedError:
            raise PermanentError("content_changed_after_verification") from None
        except ResourceNotFoundError:
            data = await storage.read_all(ORIGINALS, path)  # moved by an earlier attempt
        if await scanner.is_infected(data):
            await storage.delete(QUARANTINE, path)
            raise PermanentError("malware_detected")
        await storage.write(ORIGINALS, path, data)
        await storage.delete(QUARANTINE, path)
        try:
            await _cas(job, S.SCANNING, S.EXTRACTING, sha256=hashlib.sha256(data).hexdigest())
        except Abandoned:
            # Deleted while we were scanning: cleanup may already have run, so remove
            # the copy we just wrote instead of leaving bytes behind.
            await storage.delete(ORIGINALS, path)
            raise
        status = S.EXTRACTING

    if status == S.EXTRACTING:
        data = await storage.read_all(ORIGINALS, path)
        pages = await asyncio.to_thread(extract_pages, doc.mime, data, get_settings().max_pages)
        async with worker_unit_of_work(job.tenant_id) as s:
            await s.execute(text("DELETE FROM document_pages WHERE document_id = :d"),
                            {"d": job.document_id})
            await s.execute(text(
                "INSERT INTO document_pages (tenant_id, document_id, page_no, text) "
                "VALUES (:t, :d, :n, :x)"),
                [{"t": job.tenant_id, "d": job.document_id, "n": i, "x": p}
                 for i, p in enumerate(pages, start=1)])
            res = await s.execute(text(
                "UPDATE documents SET status = 'redacting', page_count = :n, updated_at = now() "
                "WHERE id = :id AND status = 'extracting' AND version = :v"),
                {"n": len(pages), "id": job.document_id, "v": job.document_version})
            if res.rowcount != 1:
                raise Abandoned("document left extracting")  # rolls back the page writes
        status = S.REDACTING

    for stage in ORDER[ORDER.index(status):] if status in ORDER else []:
        await STAGES[stage](job)


async def run_cleanup(job: Job, storage: BlobStorage) -> None:
    """Remove index, extracted text and bytes; then mark deleted. Idempotent."""
    async with worker_unit_of_work(job.tenant_id) as s:
        doc = (await s.execute(text(
            "SELECT status, version FROM documents WHERE id = :id"),
            {"id": job.document_id})).one_or_none()
    if doc is None or doc.version != job.document_version:
        raise Abandoned("document missing or superseded")
    if doc.status == S.DELETED:
        return
    if doc.status != S.DELETING:
        raise Abandoned(f"document is {doc.status}")
    async with worker_unit_of_work(job.tenant_id) as s:
        for table in ("document_chunks", "document_pages"):
            await s.execute(text(f"DELETE FROM {table} WHERE document_id = :d"),
                            {"d": job.document_id})
    for v in range(1, job.document_version + 1):  # every byte version ever written
        for container in (QUARANTINE, ORIGINALS):
            await storage.delete(container, f"{job.tenant_id}/{job.document_id}/v{v}")
    async with worker_unit_of_work(job.tenant_id) as s:
        stored = (await s.execute(text(
            "SELECT COALESCE(sum(quantity), 0) FROM usage_ledger "
            "WHERE ref_id = :d AND kind = 'storage_bytes'"), {"d": job.document_id})).scalar_one()
        if stored > 0:  # give the storage quota back exactly once
            await s.execute(text(
                "INSERT INTO usage_ledger (tenant_id, kind, quantity, ref_id) "
                "VALUES (:t, 'storage_bytes', :q, :d)"),
                {"t": job.tenant_id, "q": -stored, "d": job.document_id})
        res = await s.execute(text(
            "UPDATE documents SET status = 'deleted', updated_at = now() "
            "WHERE id = :id AND status = 'deleting' AND version = :v"),
            {"id": job.document_id, "v": job.document_version})
        if res.rowcount != 1:
            raise Abandoned("document left deleting")  # rolls back the quota refund


async def process_one(storage: BlobStorage | None = None,
                      scanner: Scanner | None = None) -> bool:
    """Claim and run at most one job. Returns False when the queue is empty."""
    job = await claim()
    if job is None:
        return False
    with tracer.start_as_current_span("worker.job", attributes={
            "job.id": job.id, "job.kind": job.kind, "job.attempt": job.attempts,
            "tenant.id": str(job.tenant_id)}) as span:
        outcome = await _run_job(job, storage, scanner)
        span.set_attribute("job.outcome", outcome)
        jobs_total.add(1, {"kind": job.kind, "outcome": outcome})
    return True


async def _run_job(job: Job, storage: BlobStorage | None, scanner: Scanner | None) -> str:
    try:
        if job.kind == CLEANUP:
            await run_cleanup(job, storage or get_storage())
        else:
            await run_ingest(job, storage or get_storage(), scanner or EicarScanner())
        await _finish(job, "succeeded")
        log.info("job %s succeeded doc=%s", job.id, job.document_id)
        return "succeeded"
    except Abandoned as e:
        await _finish(job, "abandoned", str(e))
        log.info("job %s abandoned: %s", job.id, e)
        return "abandoned"
    except PermanentError as e:
        await _fail_document(job, e.reason)
        await _finish(job, "failed", e.reason)
        log.warning("job %s failed permanently: %s", job.id, e.reason)
        return "failed"
    except Exception as e:  # noqa: BLE001 - transient by default; classified below
        err = f"{type(e).__name__}"  # never log content or URLs
        if job.attempts >= job.max_attempts:
            await _fail_document(job, "cleanup_failed" if job.kind == CLEANUP
                                 else "processing_failed_after_retries")
            await _finish(job, "dead", err)
            log.error("job %s dead-lettered after %s attempts: %s", job.id, job.attempts, err)
            return "dead"
        else:
            delay = backoff_s(job.attempts)
            await _finish(job, "queued", err, retry_in_s=delay)
            log.warning("job %s retry in %.1fs (attempt %s): %s",
                        job.id, delay, job.attempts, err)
            return "retry"


async def main() -> None:
    setup_logging("documind-worker")
    setup_tracing("documind-worker")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows dev shells
            pass
    poll = get_settings().worker_poll_s
    log.info("worker started")
    while not stop.is_set():
        try:
            busy = await process_one()
        except Exception:
            log.exception("worker loop error")
            busy = False
        if not busy:
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll)
            except TimeoutError:
                pass
    log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
