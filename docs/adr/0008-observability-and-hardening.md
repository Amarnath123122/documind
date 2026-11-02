# ADR-0008: Observability with privacy limits; supply-chain checks

**Status:** Accepted (Phase 4)

## Decision
- **Logs:** JSON on stdout (`ts`, `level`, `service`, `logger`, `msg`, `trace_id`, `span_id`). A `ScrubFilter` on the handler redacts SAS `sig=`, storage `AccountKey=`, `dm_session`, CSRF tokens and passwords in DB URLs from messages, arguments and tracebacks. It is a backstop: the code never logs URLs, content or prompts in the first place.
- **Traces and metrics:** OpenTelemetry SDK, OTLP over HTTP, **off unless `OTEL_ENDPOINT` is set**. Spans: API requests (FastAPI instrumentation; health probes excluded), `worker.job` (id, kind, attempt, tenant, outcome) and `chat.generate` (evidence count, citations, grounding, provider, token counts). Metrics: `documind.jobs{kind,outcome}` and `documind.chat.duration`. Attributes never include document text, questions or answers.
- **Request IDs:** the real trace id when tracing is on. A caller's `X-Request-ID` is accepted only if it matches `[A-Za-z0-9-]{8,64}`, which prevents log or header injection.
- **Local:** `dev.ps1 observe` adds Jaeger (UI on :16686). **Production:** point `OTEL_ENDPOINT` at an OpenTelemetry Collector that forwards to Azure Monitor / Application Insights. The application code does not change.
- **Supply chain:** CI runs `pip-audit` and `npm audit --omit=dev`, and fails on known vulnerabilities. The image upgrades `pip`. Dependency pinning with lockfiles (`uv.lock`) comes with the platform phase.

## Verified
Jaeger received `documind-api` traces including `chat.generate`, with id, count and outcome attributes only. The worker emits JSON logs. Baseline latency is in `docs/benchmarks/2026-11-02-local-latency.md`.

## Not yet
Alert rules (dead jobs > 0, `needs_attention` > 0, 5xx rate, chat p95) and dashboards are defined in the platform phase alongside the deployment target.
