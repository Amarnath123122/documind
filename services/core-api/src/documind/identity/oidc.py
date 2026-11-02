"""OIDC Authorization Code + PKCE, server side (the API is the BFF, ADR-0009).

The browser never sees tokens: the API exchanges the code, validates the ID token, maps
(issuer, subject) to a provisioned DocuMind user and issues its own session cookie.
Provider tokens are discarded: DocuMind does not call APIs on the user's behalf.
"""

import asyncio
import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt
from itsdangerous import BadSignature, URLSafeTimedSerializer

from documind.common.config import get_settings

FLOW_COOKIE = "dm_oidc"
FLOW_TTL_S = 600
ALLOWED_ALGS = ["RS256", "ES256", "PS256"]  # never "none" or HMAC with a public key


class OidcError(Exception):
    """Any login failure. The message is for logs only, never for the response body."""


@dataclass(frozen=True)
class Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


@dataclass(frozen=True)
class Flow:
    state: str
    nonce: str
    verifier: str


_cache: dict[str, tuple[float, Discovery, jwt.PyJWKClient]] = {}


async def discover(issuer: str) -> tuple[Discovery, jwt.PyJWKClient]:
    hit = _cache.get(issuer)
    if hit and hit[0] > time.monotonic():
        return hit[1], hit[2]
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
        r.raise_for_status()
        doc = r.json()
    d = Discovery(doc["issuer"], doc["authorization_endpoint"], doc["token_endpoint"],
                  doc["jwks_uri"])
    if d.issuer.rstrip("/") != issuer.rstrip("/"):
        raise OidcError("discovery issuer mismatch")
    jwks = jwt.PyJWKClient(d.jwks_uri, cache_keys=True, lifespan=3600)
    _cache[issuer] = (time.monotonic() + 3600, d, jwks)
    return d, jwks


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt="documind.oidc-flow")


def new_flow() -> tuple[Flow, str]:
    """Fresh state/nonce/PKCE verifier and the signed cookie value that carries them."""
    f = Flow(secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64))
    return f, _signer().dumps({"s": f.state, "n": f.nonce, "v": f.verifier})


def read_flow(cookie: str | None) -> Flow:
    if not cookie:
        raise OidcError("missing flow cookie")
    try:
        d = _signer().loads(cookie, max_age=FLOW_TTL_S)
    except BadSignature:
        raise OidcError("invalid or expired flow cookie") from None
    return Flow(d["s"], d["n"], d["v"])


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(d: Discovery, f: Flow) -> str:
    s = get_settings()
    return d.authorization_endpoint + "?" + urlencode({
        "response_type": "code", "client_id": s.oidc_client_id,
        "redirect_uri": s.oidc_redirect_uri, "scope": "openid email profile",
        "state": f.state, "nonce": f.nonce,
        "code_challenge": pkce_challenge(f.verifier), "code_challenge_method": "S256"})


def validate_id_token(token: str, *, key_for: Callable[[str], object], issuer: str,
                      audience: str, nonce: str, leeway_s: int = 60) -> dict:
    """Signature, alg allow-list, iss, aud, exp/iat, nonce. Raises OidcError on any failure."""
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in ALLOWED_ALGS:
            raise OidcError(f"disallowed alg {header.get('alg')}")
        claims = jwt.decode(token, key=key_for(token), algorithms=ALLOWED_ALGS,
                            audience=audience, issuer=issuer, leeway=leeway_s,
                            options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except jwt.PyJWTError as e:
        raise OidcError(f"id_token rejected: {type(e).__name__}") from None
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OidcError("nonce mismatch")
    return claims


async def complete(code: str, state: str, flow: Flow) -> dict:
    if not secrets.compare_digest(state, flow.state):
        raise OidcError("state mismatch")
    s = get_settings()
    d, jwks = await discover(s.oidc_issuer)
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(d.token_endpoint, data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": s.oidc_redirect_uri, "code_verifier": flow.verifier,
            "client_id": s.oidc_client_id, "client_secret": s.oidc_client_secret})
    if r.status_code != 200 or "id_token" not in r.json():
        raise OidcError(f"token endpoint returned {r.status_code}")
    id_token = r.json()["id_token"]
    signing_key = await asyncio.to_thread(jwks.get_signing_key_from_jwt, id_token)
    return validate_id_token(id_token, key_for=lambda _t: signing_key.key, issuer=d.issuer,
                             audience=s.oidc_client_id, nonce=flow.nonce)
