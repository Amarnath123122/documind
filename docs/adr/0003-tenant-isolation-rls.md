# ADR-0003: Tenant isolation via membership-derived RLS

**Status:** Accepted (Phase 0)

**Decision.**
- Three DB roles: `postgres` (local seed only), `documind_owner` (owns schema, runs migrations), `documind_app` (runtime: `NOSUPERUSER NOBYPASSRLS`, not owner).
- Every table has `ENABLE` + `FORCE ROW LEVEL SECURITY`.
- The app sets **only** `app.user_id`, transaction-local, from the verified session. Tenants are *derived* in SQL: `tenant_id IN (SELECT app_tenant_ids())`. A client can never put a tenant id into the RLS context.
- Policies have `WITH CHECK`, so cross-tenant INSERT/UPDATE also fail.
- `audit_events` is append-only for the app role.
- RLS covers *tenant* scope; role permissions are checked in the domain (`Principal.can`). Document-level grants (ReBAC) arrive with sharing.

**Proof.** `tests/security/test_tenant_isolation.py` runs raw SQL as the app role and covers cross-tenant insert/update, anonymous access, pooled-connection reuse and the role's privilege flags.

**Trade-off.** `app_tenant_ids()` is a `SECURITY DEFINER` subquery per statement. It is fine at this scale; benchmark it with `EXPLAIN (ANALYZE, BUFFERS)` before hardening.
