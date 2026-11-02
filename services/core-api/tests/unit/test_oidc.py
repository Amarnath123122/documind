"""ID token validation without any network: locally generated keys."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from documind.identity.oidc import OidcError, new_flow, pkce_challenge, read_flow, validate_id_token

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISS, AUD, NONCE = "https://idp.example", "documind-web", "n-123"


def token(key=KEY, alg="RS256", **over) -> str:
    now = int(time.time())
    claims = {"iss": ISS, "aud": AUD, "sub": "u1", "iat": now, "exp": now + 300,
              "nonce": NONCE} | over
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm=alg)


def check(tok: str, nonce: str = NONCE) -> dict:
    return validate_id_token(tok, key_for=lambda _t: KEY.public_key(), issuer=ISS,
                             audience=AUD, nonce=nonce)


def test_valid_token_accepted():
    assert check(token())["sub"] == "u1"


@pytest.mark.parametrize("bad", [
    {"aud": "someone-else"}, {"iss": "https://evil.example"}, {"exp": 1000, "iat": 900},
    {"nonce": "replayed"}, {"sub": None}, {"exp": None},
])
def test_bad_claims_rejected(bad):
    with pytest.raises(OidcError):
        check(token(**bad))


def test_wrong_signing_key_rejected():
    with pytest.raises(OidcError):
        check(token(key=OTHER))


def test_alg_none_rejected():
    unsigned = jwt.encode({"iss": ISS, "aud": AUD, "sub": "u", "iat": 1, "exp": 2**31,
                           "nonce": NONCE}, None, algorithm="none")
    with pytest.raises(OidcError):
        check(unsigned)


def test_hmac_with_public_key_confusion_rejected():
    forged = jwt.encode({"iss": ISS, "aud": AUD, "sub": "u", "iat": int(time.time()),
                         "exp": int(time.time()) + 60, "nonce": NONCE},
                        "any-shared-secret-of-sufficient-length-123", algorithm="HS256")
    with pytest.raises(OidcError):
        check(forged)


def test_flow_cookie_roundtrip_and_tamper():
    flow, cookie = new_flow()
    assert read_flow(cookie) == flow
    with pytest.raises(OidcError):
        read_flow(cookie[:-2] + "xx")
    with pytest.raises(OidcError):
        read_flow(None)


def test_pkce_s256_known_vector():
    # RFC 7636 appendix B
    assert pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
