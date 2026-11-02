# DocuMind threat model (STRIDE, as built through Phase 4)

**Assets:** tenant documents and extracted text, redacted values, chat history, session cookies, upload capabilities (SAS), audit log, quotas.
**Trust boundaries:** browser ↔ web origin/API · API ↔ Postgres (app role) · worker ↔ Postgres (worker role) · browser ↔ blob storage (SAS) · API ↔ LLM provider · documents ↔ prompt (untrusted content).

| # | Threat | Control as built | Test / evidence | Residual risk / next step |
|---|---|---|---|---|
| S1 | Forged or stolen session | Signed HttpOnly SameSite=Strict cookie, 8 h max age | `test_forged_cookie_is_401` | No server-side session revocation list yet; add a session table with OIDC/BFF |
| S2 | Dev login in production | `Settings` refuses `ENV=production` + `AUTH_MODE=dev`, weak secret, or Azurite | `test_production_rejects_dev_auth` | — |
| T1 | CSRF on mutations | Double-submit token bound to the signed session | `test_csrf_required_for_mutations` | — |
| T2 | Upload type or size spoofing | Size and magic-byte checks on the **stored** bytes | `test_spoofed_content_type_rejected`, `test_size_mismatch_*` | Deep structural validation per format |
| T3 | Bytes swapped after verification (SAS still live) | ETag pinned at complete; worker reads with If-Match | `test_blob_overwritten_after_verification_is_rejected` | Shorter SAS TTL once resumable uploads exist |
| T4 | Malicious file attacks parsers | Quarantine, scan before parse, zip-bomb/page/encryption limits, parsing off the API process | `test_malware_*`, `test_docx_zip_bomb_rejected` | Run the worker in a stricter sandbox (seccomp, no egress); real AV engine |
| R1 | Denying privileged actions | Append-only `audit_events` (no UPDATE/DELETE grant) in the same transaction as the change | `test_audit_is_append_only_for_app_role` | Ship audit to immutable storage |
| I1 | Cross-tenant read via guessed ID | RLS derived from verified `app.user_id`; IDs are selectors; 404 for foreign and missing alike | `test_tenant_isolation.py` (17 tests) | — |
| I2 | Bug in application filters | Same RLS applies to raw SQL; runtime roles are not owner, superuser or BYPASSRLS | `test_rls_*`, `test_app_role_is_not_privileged` | Periodic role audit in production |
| I3 | Restricted doc leaks via search, chat or citations | Visibility rule lives in RLS; chunks inherit it; citations re-validated on read | `test_restricted_document_hidden_everywhere_until_granted`, `test_revoked_grant_*` | Already-displayed answers cannot be recalled (by design) |
| I4 | PII sent to the model or index | Redaction before chunking; raw pages readable only by the worker; context built from chunks | `test_model_context_never_contains_other_tenant_or_raw_pii` | Regex recall is limited: evaluate Presidio/NER on a labelled set |
| I5 | Secrets in logs | Never log URLs or content; scrubbing filter on every record; error bodies are RFC 9457 without internals | `test_scrub_*`, `test_unsafe_request_id_is_not_echoed` | Log retention policy in production |
| I6 | Prompt injection exfiltrates data | No tools; evidence fenced as untrusted data; context limited to the caller's authorized chunks; citations validated | `test_context_fences_*`, `test_hallucinated_citation_*` | A real LLM may still produce misleading prose: user-facing grounding labels and evals |
| D1 | Cost or resource exhaustion | Monthly token and storage quotas (429), upload size cap, page cap, context and output budgets, 90 s generation timeout | `test_quota_exhaustion_returns_429` | Soft quota overshoot; add per-user rate limiting (Redis or the gateway) |
| D2 | Poison job blocks the queue | Lease + SKIP LOCKED, bounded retries, dead letter, permanent-vs-transient classification | `test_dead_letter_*`, `test_expired_lease_is_reclaimed` | Alert on dead and `needs_attention` counts |
| E1 | Worker abused to read other tenants | Worker role sees only the claimed job's tenant; no identity or audit access | `test_worker_role_*` | — |
| E2 | Deleted content revived by a retry | Version bump + compare-and-set; writer deletes its own late blob | `test_delete_during_ingest_never_resurrects`, `test_deleting_document_is_never_revived` | — |
