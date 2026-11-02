"""Session cookie + CSRF (double-submit, bound to the signed session).

Phase 0 runs the cookie session inside the API behind the same-origin dev proxy;
ADR-0004 explains when it moves to a dedicated BFF with OIDC.
"""

import hmac
import secrets
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from documind.common.config import get_settings
from documind.common.db import unit_of_work
from documind.common.errors import Forbidden, Unauthenticated
from documind.identity.domain import Principal
from documind.identity.repository import load_principal

SESSION_COOKIE = "dm_session"
CSRF_COOKIE = "dm_csrf"
CSRF_HEADER = "X-CSRF-Token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt="documind.session")


def issue_session(response: Response, user_id: UUID) -> None:
    s = get_settings()
    csrf = secrets.token_urlsafe(32)
    token = _serializer().dumps({"uid": str(user_id), "csrf": csrf})
    secure = s.env == "production"
    response.set_cookie(SESSION_COOKIE, token, max_age=s.session_max_age_s,
                        httponly=True, secure=secure, samesite="strict", path="/")
    # Readable by the SPA so it can echo it in the header; useless to a cross-site attacker.
    response.set_cookie(CSRF_COOKIE, csrf, max_age=s.session_max_age_s,
                        httponly=False, secure=secure, samesite="strict", path="/")


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def _read_session(request: Request) -> dict:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise Unauthenticated("Sign in required.")
    try:
        return _serializer().loads(raw, max_age=get_settings().session_max_age_s)
    except BadSignature:
        raise Unauthenticated("Session invalid or expired.") from None


async def get_principal(request: Request) -> Principal:
    data = _read_session(request)
    if request.method not in _SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(sent, data["csrf"]):
            raise Forbidden("CSRF token missing or invalid.", code="CSRF_FAILED")
    user_id = UUID(data["uid"])
    async with unit_of_work(user_id) as session:
        principal = await load_principal(session, user_id)
    if principal is None:
        raise Unauthenticated("Account no longer exists.")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
