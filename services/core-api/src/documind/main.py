"""Composition root: wires modules into one deployable (modular monolith, ADR-0001)."""

import re
import uuid

from fastapi import FastAPI, Request
from sqlalchemy import text

from documind.chat.api import router as chat_router
from documind.common.config import get_settings
from documind.common.db import get_engine
from documind.common.errors import install_error_handlers
from documind.common.telemetry import current_trace_id, setup_logging, setup_tracing
from documind.documents.api import router as documents_router
from documind.identity.api import router as identity_router
from documind.retrieval.api import router as search_router
from documind.usage.api import router as usage_router
from documind.workflows.api import router as workflows_router

_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def create_app() -> FastAPI:
    get_settings()  # fail fast on unsafe production config
    setup_logging("documind-api")
    setup_tracing("documind-api")
    app = FastAPI(title="DocuMind Core API", version="0.1.0")
    install_error_handlers(app)

    @app.middleware("http")
    async def _trace_and_headers(request: Request, call_next):
        # Prefer the real trace id; accept a caller's id only if it is safe to log.
        supplied = request.headers.get("X-Request-ID", "")
        request.state.trace_id = (current_trace_id()
                                  or (supplied if _REQUEST_ID.match(supplied) else None)
                                  or uuid.uuid4().hex)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.trace_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz", tags=["ops"])
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz():
        async with get_engine().connect() as c:
            await c.execute(text("SELECT 1"))
        return {"status": "ready"}

    app.include_router(identity_router)
    app.include_router(documents_router)
    app.include_router(search_router)
    app.include_router(chat_router)
    app.include_router(usage_router)
    app.include_router(workflows_router)
    if get_settings().otel_endpoint:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")
    return app


app = create_app()
