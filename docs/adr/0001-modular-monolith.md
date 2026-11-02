# ADR-0001: Modular monolith first

**Status:** Accepted (Phase 0)

**Context.** The original prompt called for 15 services, micro-frontends, a "strangler" migration and a service mesh. We have one team and no measured scaling hotspot.

**Decision.** One FastAPI deployable (`services/core-api`) with module boundaries (`identity`, `documents`, later `privacy`, `retrieval`, `chat`, …). Each module has a pure `domain.py` with no framework imports, a repository adapter, a service (use cases) and an API router. Background workers (Phase 1+) are separate *processes* from the same codebase, so they scale independently without being separate services.

**Enforcement.** `tests/unit/test_domain.py::test_domain_has_no_framework_imports` fails the build if a domain module imports FastAPI/SQLAlchemy/Pydantic/Azure.

**Revisit when.** A module needs an independent release cadence, owning team, or scaling profile that we have *measured*.
