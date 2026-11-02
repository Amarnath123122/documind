# ADR-0002: Transactional outbox + job table, not Kafka

**Status:** Accepted (implemented in Phase 1)

**Context.** Ingestion needs leases, retries with backoff, dead-lettering and job status. It does not need replayable ordered streams or multiple consumer groups yet.

**Decision.** A Postgres `jobs` table is written in the same transaction as the domain change, and workers claim jobs with `FOR UPDATE SKIP LOCKED` plus a lease timeout. Delivery is assumed at-least-once, so every effect is idempotent. The publisher sits behind a port, so Kafka/Event Hubs can replace it later without changing domain events.

**Revisit when.** Analytics/audit streaming needs replay or fan-out to independent consumers.
