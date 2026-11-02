import logging
from uuid import UUID

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from documind.common.config import get_settings
from documind.common.db import unit_of_work
from documind.common.errors import NotFound, Unauthenticated
from documind.identity import oidc
from documind.identity.repository import resolve_oidc_user, resolve_user_id
from documind.identity.session import CurrentPrincipal, clear_session, issue_session

router = APIRouter(prefix="/v1", tags=["identity"])
log = logging.getLogger("documind.auth")


class DevLogin(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class MembershipOut(BaseModel):
    tenant_id: UUID
    tenant_name: str
    role: str
    permissions: list[str]


class MeOut(BaseModel):
    user_id: UUID
    email: str
    display_name: str
    memberships: list[MembershipOut]


@router.post("/dev/login", status_code=204)
async def dev_login(body: DevLogin, response: Response) -> None:
    """Deterministic local identity. Disabled unless AUTH_MODE=dev (never in production)."""
    if get_settings().auth_mode != "dev":
        raise NotFound()
    async with unit_of_work(None) as session:
        user_id = await resolve_user_id(session, body.email)
    if user_id is None:
        raise Unauthenticated("Unknown dev user.")
    issue_session(response, user_id)


@router.post("/auth/logout", status_code=204)
async def logout(response: Response) -> None:
    clear_session(response)


@router.get("/me", response_model=MeOut)
async def me(principal: CurrentPrincipal) -> MeOut:
    return MeOut(
        user_id=principal.user_id,
        email=principal.email,
        display_name=principal.display_name,
        memberships=[
            MembershipOut(tenant_id=m.tenant_id, tenant_name=m.tenant_name, role=m.role,
                          permissions=sorted(m.permissions))
            for m in principal.memberships
        ],
    )


@router.get("/auth/config")
async def auth_config() -> dict:
    """Lets the SPA show dev sign-in buttons or the single-sign-on link."""
    return {"mode": get_settings().auth_mode}


@router.get("/auth/login")
async def oidc_login() -> RedirectResponse:
    s = get_settings()
    if s.auth_mode != "oidc":
        raise NotFound()
    discovery, _ = await oidc.discover(s.oidc_issuer)
    flow, cookie = oidc.new_flow()
    resp = RedirectResponse(oidc.authorize_url(discovery, flow), status_code=302)
    # Lax (not Strict): it must ride along on the IdP's top-level redirect back to us.
    resp.set_cookie(oidc.FLOW_COOKIE, cookie, max_age=oidc.FLOW_TTL_S, httponly=True,
                    secure=s.env == "production", samesite="lax", path="/v1/auth")
    return resp


@router.get("/auth/callback")
async def oidc_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    s = get_settings()
    if s.auth_mode != "oidc":
        raise NotFound()
    try:
        flow = oidc.read_flow(request.cookies.get(oidc.FLOW_COOKIE))
        claims = await oidc.complete(code, state, flow)
    except (oidc.OidcError, httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("oidc login rejected: %s", e)
        raise Unauthenticated("Sign-in failed.", code="OIDC_FAILED") from None
    async with unit_of_work(None) as session:
        user_id = await resolve_oidc_user(session, claims["iss"], claims["sub"],
                                          claims.get("email"),
                                          claims.get("email_verified") is True)
    if user_id is None:
        log.warning("oidc login for unprovisioned identity")
        raise Unauthenticated("Your account has not been provisioned.", code="NOT_PROVISIONED")
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(oidc.FLOW_COOKIE, path="/v1/auth")  # single use
    issue_session(resp, user_id)
    return resp
