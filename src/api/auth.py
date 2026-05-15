"""Authentication primitives for the control API.

Password hashes live in SQLite (see `users.py`) and sessions are stateless JWTs
signed with `AUTH_SECRET`. No secret = the API refuses to issue or accept
tokens, so a misconfigured VPS fails closed instead of accepting anything.
"""
from __future__ import annotations

import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .users import UserStore

_ALGO = "HS256"
_DEFAULT_TTL_S = 60 * 60  # 1h
# bcrypt truncates to 72 bytes; longer passwords collide. Reject instead of silently truncating.
_MAX_PASSWORD_BYTES = 72

_bearer = HTTPBearer(auto_error=False)

# Pre-computed hash of an impossible password — used by authenticate() to make
# the unknown-user path take roughly the same time as the known-user path, so
# response timing can't be used to enumerate usernames.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password-never-matches", bcrypt.gensalt(rounds=12))


def _secret() -> str:
    s = os.environ.get("AUTH_SECRET")
    if not s or len(s) < 32:
        raise RuntimeError(
            "AUTH_SECRET env var must be set to a 32+ char random string. "
            "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    return s


def hash_password(plaintext: str) -> str:
    pw_bytes = plaintext.encode("utf-8")
    if len(pw_bytes) > _MAX_PASSWORD_BYTES:
        raise ValueError(f"password too long ({len(pw_bytes)} bytes, max {_MAX_PASSWORD_BYTES})")
    return bcrypt.hashpw(pw_bytes, bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plaintext: str, hashed: str) -> bool:
    pw_bytes = plaintext.encode("utf-8")
    if len(pw_bytes) > _MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(pw_bytes, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_token(username: str, role: str = "admin", ttl_s: int = _DEFAULT_TTL_S) -> tuple[str, int]:
    now = int(time.time())
    payload = {
        "sub": username,
        "role": role,
        "iat": now,
        "exp": now + ttl_s,
        "jti": secrets.token_urlsafe(8),
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGO), now + ttl_s


def decode_token(token: str) -> dict[str, str]:
    """Return {"username": ..., "role": ...}. Raises 401 on any failure."""
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from None
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed token")
    # Tokens minted before roles existed default to admin so existing sessions
    # keep working through the rollout. New tokens always carry an explicit role.
    role = payload.get("role") or "admin"
    # Legacy 'viewer' tokens are accepted and normalized to 'user' so sessions
    # minted before the rename keep working until they expire on their own.
    if role == "viewer":
        role = "user"
    if role not in ("admin", "user"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed token")
    return {"username": sub, "role": role}


@dataclass
class LoginRateLimiter:
    """In-memory sliding window: N attempts per `window_s` per client IP.

    Lives in the API process. Good enough for a single-node VPS; swap for Redis
    if the API ever scales horizontally.
    """
    max_attempts: int = 5
    window_s: int = 15 * 60
    _hits: dict[str, deque[float]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._hits = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.time()
        q = self._hits[key]
        cutoff = now - self.window_s
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= self.max_attempts:
            retry = int(q[0] + self.window_s - now) + 1
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"too many login attempts, retry in {retry}s",
            )

    def record(self, key: str) -> None:
        self._hits[key].append(time.time())

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


_rate_limiter = LoginRateLimiter()


def rate_limiter() -> LoginRateLimiter:
    return _rate_limiter


def authenticate(store: UserStore, username: str, password: str) -> str | None:
    """Returns the user's role on success, None on failure.

    Dummy-hashes on unknown username so response time doesn't leak existence.
    """
    h = store.get_hash(username)
    if h is None:
        verify_password(password, _DUMMY_HASH.decode("utf-8"))
        return None
    if not verify_password(password, h):
        return None
    return store.get_role(username) or "admin"


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, str]:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return decode_token(creds.credentials)


def require_admin(user: dict[str, str] = Depends(current_user)) -> dict[str, str]:
    if user.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin privileges required")
    return user


def _trusted_proxies() -> frozenset[str]:
    raw = os.environ.get("TRUSTED_PROXIES", "")
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def client_ip(request: Request) -> str:
    """Return the originating client IP for rate-limit bucketing.

    Only honors X-Forwarded-For when the immediate peer is listed in
    TRUSTED_PROXIES — otherwise an attacker could spoof the header to
    rotate buckets and bypass the per-IP login-attempt cap. Empty
    TRUSTED_PROXIES means "no proxy in front", so we use the peer IP.
    """
    peer = request.client.host if request.client else "unknown"
    trusted = _trusted_proxies()
    if peer in trusted:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # First entry is the original client; subsequent entries are
            # intermediate proxies (RFC 7239 left-to-right).
            return xff.split(",")[0].strip()
    return peer
