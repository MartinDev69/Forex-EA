"""JWT-backed setup tokens for password creation.

Tokens are HS256-signed with AUTH_SECRET, carry the target AD-ID + email,
expire after 24h, and are single-use (the jti is burned in SQLite on redeem).
The mailer turns these into clickable links at PUBLIC_BASE_URL/setup?token=...
"""
from __future__ import annotations

import secrets
import time
from typing import TypedDict

import jwt

from .auth import _secret
from .mailer import public_base_url

_ALGO = "HS256"
SETUP_TTL_S = 24 * 60 * 60


class SetupClaims(TypedDict):
    ad_id: str
    email: str
    jti: str
    exp: int


def create_setup_token(ad_id: str, email: str, ttl_s: int = SETUP_TTL_S) -> tuple[str, int, str]:
    """Returns (jwt, expires_at_unix, full_url)."""
    now = int(time.time())
    exp = now + ttl_s
    jti = secrets.token_urlsafe(12)
    payload = {"ad_id": ad_id, "email": email, "iat": now, "exp": exp, "jti": jti,
               "typ": "setup"}
    token = jwt.encode(payload, _secret(), algorithm=_ALGO)
    url = f"{public_base_url()}/setup?token={token}"
    return token, exp, url


def decode_setup_token(token: str) -> SetupClaims:
    """Raises jwt.InvalidTokenError / ExpiredSignatureError on any failure."""
    payload = jwt.decode(token, _secret(), algorithms=[_ALGO])
    if payload.get("typ") != "setup":
        raise jwt.InvalidTokenError("not a setup token")
    if not all(k in payload for k in ("ad_id", "email", "jti", "exp")):
        raise jwt.InvalidTokenError("missing claims")
    return {
        "ad_id": payload["ad_id"],
        "email": payload["email"],
        "jti": payload["jti"],
        "exp": int(payload["exp"]),
    }
