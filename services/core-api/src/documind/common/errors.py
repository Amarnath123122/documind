"""RFC 9457 Problem Details. Public bodies never contain stack traces, SQL or other tenants' data."""

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("documind")


class AppError(Exception):
    status = 500
    code = "INTERNAL"
    title = "Internal Server Error"

    def __init__(self, detail: str = "", code: str | None = None):
        super().__init__(detail)
        self.detail = detail
        if code:
            self.code = code


class Unauthenticated(AppError):
    status, code, title = 401, "UNAUTHENTICATED", "Unauthenticated"


class Forbidden(AppError):
    status, code, title = 403, "FORBIDDEN", "Forbidden"


class NotFound(AppError):
    """Also used for objects that exist but are invisible to the actor (no existence leak)."""

    status, code, title = 404, "NOT_FOUND", "Not Found"


class BadRequest(AppError):
    status, code, title = 400, "BAD_REQUEST", "Bad Request"


def _problem(status: int, title: str, code: str, detail: str, trace_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={
            "type": f"https://docs.documind.invalid/problems/{code.lower()}",
            "title": title,
            "status": status,
            "detail": detail,
            "code": code,
            "trace_id": trace_id,
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        return _problem(exc.status, exc.title, exc.code, exc.detail, _trace(request))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
        return _problem(422, "Unprocessable Content", "VALIDATION_FAILED",
                        f"Invalid fields: {fields}", _trace(request))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        trace = _trace(request)
        log.exception("unhandled error trace_id=%s", trace)
        return _problem(500, "Internal Server Error", "INTERNAL", "Unexpected error.", trace)


def _trace(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or uuid.uuid4().hex
