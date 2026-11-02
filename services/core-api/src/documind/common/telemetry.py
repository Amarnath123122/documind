"""Logs, traces and metrics, with privacy limits.

- JSON logs carry service + trace_id so a log line links to its trace.
- A scrubbing filter removes bearer material (SAS signatures, session cookies, storage keys,
  passwords in URLs) from every record, as a last line of defense behind "don't log it".
- Traces export over OTLP only when OTEL_ENDPOINT is set; otherwise tracing is a no-op.
  Span attributes are ids, counts and outcomes. Never document text, prompts or answers.
"""

import json
import logging
import re
import sys

from opentelemetry import metrics, trace

from documind.common.config import get_settings

_SECRETS = [
    (re.compile(r"(sig=)[^&\s\"']+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(AccountKey=)[^;\s\"']+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(dm_session=)[^;\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(X-CSRF-Token[\"']?\s*[:=]\s*[\"']?)[\w-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:)[^@\s]+(@)"), r"\1[REDACTED]\2"),
]


def scrub(text: str) -> str:
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    return text


class ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = ()
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        ctx = trace.get_current_span().get_span_context()
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
               "service": self.service, "logger": record.name, "msg": record.getMessage()}
        if ctx.is_valid:
            out["trace_id"] = format(ctx.trace_id, "032x")
            out["span_id"] = format(ctx.span_id, "016x")
        if record.exc_text:
            out["exc"] = record.exc_text
        return json.dumps(out, ensure_ascii=False)


def setup_logging(service: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(ScrubFilter())
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for noisy in ("azure", "httpx", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def setup_tracing(service: str) -> None:
    endpoint = get_settings().otel_endpoint
    if not endpoint:
        return
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": service,
                                "deployment.environment": get_settings().env})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")))
    trace.set_tracer_provider(provider)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[
        PeriodicExportingMetricReader(OTLPMetricExporter(
            endpoint=f"{endpoint.rstrip('/')}/v1/metrics"), export_interval_millis=15000)]))


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


tracer = trace.get_tracer("documind")
meter = metrics.get_meter("documind")
jobs_total = meter.create_counter("documind.jobs", description="Worker job outcomes")
chat_latency = meter.create_histogram("documind.chat.duration", unit="s",
                                      description="Chat turn generation time")
