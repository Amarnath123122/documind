# ADR-0007: Restricted documents in RLS, deny-first deletion, usage quotas

**Status:** Accepted (Phase 3)

## Restricted documents and sharing (RBAC + limited ReBAC)
- `documents.visibility` is `workspace` (the default: every tenant member with `document:read`) or `restricted` (the creator, tenant admins, and users with an active `sharing_grants` row).
- The rule lives **in the `documents` RLS policy**. `document_chunks` is visible only when its document row is visible, so list, get, search, chat retrieval, the pre-model recheck and citation re-validation all inherit it with no extra application code.
- Grants may only target **members of the same tenant** (composite foreign key to `memberships`, plus `resolve_member_by_email` scoped to the caller's tenants). Unknown and foreign emails return the same 404.
- Revocation sets `revoked_at`. There is **no permission cache**, so the next query no longer sees the document. Answers already shown cannot be recalled, but revisiting history shows the citation as `available: false` with the quote withheld.
- Who can manage a document: tenant admins, or the creator if they hold `document:create`/`document:share`. Viewers cannot.
- `SECURITY DEFINER` helpers (`app_is_tenant_admin`, `app_has_grant`, `resolve_member_by_email`) run as `documind_owner`, which gets narrow `*_definer` SELECT policies. The owner role is never used by the running app.

## Deletion
`DELETE /v1/documents/{id}` (optional `If-Match: "<version>"`, else 412) does three things in one transaction: sets `deleting`, **increments the version**, and enqueues a `cleanup` job.
- Reads, search and chat deny immediately, because only `ready` documents are served.
- Any in-flight ingest job carries the old version, so every compare-and-set it attempts fails and it abandons itself. If it already wrote scanned bytes, it deletes them on the way out.
- The cleanup worker removes chunks, pages and every blob version, credits the storage quota back exactly once, then marks the document `deleted` (a metadata tombstone kept for audit).
- When cleanup exhausts its retries, the document goes to `needs_attention`, **never** `deleted`, so nothing silently claims success.
- Legal hold and retention exceptions are future work: the tombstone design already allows them.

## Usage and quotas
- The append-only `usage_ledger` records `chat_tokens` (approximate, per turn) and `storage_bytes` (positive at upload, negative at cleanup). Postgres is the source of truth. Redis is not used for balances.
- `tenant_quotas` sets monthly tokens and total storage. A tenant with no row gets a small default (fail closed). Checks happen before work starts, and exceeding a quota returns `429 QUOTA_EXCEEDED`.
- **Known limit:** these are soft quotas. Concurrent requests can overshoot by about one request each. Hard limits need reservations (`usage_reservations`), which arrive with real provider billing.
- `GET /v1/tenants/{id}/usage` requires `usage:view` (tenant admin).
