"""Verifies the ID token Google's sign-in button gives the browser ("Continue with Google").

The token is a JWT signed by Google: the signature is checked against Google's published keys, and it must be
for this Tracebase (audience = TRACEBASE_GOOGLE_CLIENT_ID), from Google, unexpired, with a verified email."""
from __future__ import annotations

import jwt

from .config import settings

CERTS = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("accounts.google.com", "https://accounts.google.com")
_jwks: jwt.PyJWKClient | None = None


class GoogleAuthError(Exception):
    pass


def _keys() -> jwt.PyJWKClient:
    global _jwks
    if _jwks is None:
        _jwks = jwt.PyJWKClient(CERTS, cache_keys=True, lifespan=3600, timeout=10)
    return _jwks


def verify(credential: str) -> dict:
    """The verified claims ({email, name, picture, ...}), or GoogleAuthError."""
    if not settings.google_client_id:
        raise GoogleAuthError("Google sign-in is not configured on this Tracebase")
    try:
        key = _keys().get_signing_key_from_jwt(credential).key
        claims = jwt.decode(credential, key, algorithms=["RS256"], audience=settings.google_client_id,
                            options={"require": ["exp", "iat", "iss", "aud", "email"]}, leeway=30)
    except jwt.PyJWKClientError as e:
        raise GoogleAuthError(f"could not check the token with Google: {e}") from e
    except jwt.InvalidTokenError as e:
        raise GoogleAuthError(f"invalid Google sign-in token: {e}") from e
    if claims.get("iss") not in ISSUERS:
        raise GoogleAuthError("the token was not issued by Google")
    if not claims.get("email_verified"):
        raise GoogleAuthError("this Google account's email address is not verified")
    return claims
