"""TOTP (RFC 6238) — time-based one-time passwords for the 2FA gate.

No external deps. Uses HMAC-SHA1 with a 30-second step and 6-digit code,
which matches Google Authenticator / Authy / 1Password defaults. Verify
accepts ±1 step (±30s) so a code generated right at a step boundary
isn't rejected by clock skew.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

_DIGITS = 6
_STEP_S = 30
_DRIFT_STEPS = 1  # ±30s window — generous enough for phone clock skew


def generate_secret(num_bytes: int = 20) -> str:
    """Return a fresh base32-encoded secret. 20 bytes = 160 bits, the SHA-1
    HMAC block, and what RFC 6238 examples use.
    """
    raw = secrets.token_bytes(num_bytes)
    # base32 encode, strip padding so the secret is the form authenticator
    # apps expect when typed manually.
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _decode_secret(secret_b32: str) -> bytes:
    s = secret_b32.upper().replace(" ", "")
    # Re-pad — base32 requires length multiple of 8.
    pad = (-len(s)) % 8
    return base64.b32decode(s + "=" * pad, casefold=True)


def generate_code(secret_b32: str, *, at: float | None = None, step: int = _STEP_S) -> str:
    """RFC 6238 TOTP value at time `at` (defaults to now)."""
    t = int((at if at is not None else time.time()) // step)
    msg = struct.pack(">Q", t)
    digest = hmac.new(_decode_secret(secret_b32), msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    code = truncated % (10 ** _DIGITS)
    return f"{code:0{_DIGITS}d}"


def verify_code(
    secret_b32: str,
    code: str,
    *,
    at: float | None = None,
    drift_steps: int = _DRIFT_STEPS,
    step: int = _STEP_S,
) -> bool:
    """Constant-time verify across ±drift_steps windows."""
    if not code or not code.isdigit() or len(code) != _DIGITS:
        return False
    now = at if at is not None else time.time()
    target = code.encode("ascii")
    for delta in range(-drift_steps, drift_steps + 1):
        candidate = generate_code(secret_b32, at=now + delta * step, step=step).encode("ascii")
        if hmac.compare_digest(candidate, target):
            return True
    return False


def provisioning_uri(secret_b32: str, *, account: str, issuer: str = "Forex-EA") -> str:
    """otpauth:// URI for QR-code enrollment in authenticator apps.

    Authenticator apps render this directly when scanned. Format per the
    Google Authenticator key-uri spec.
    """
    label = f"{issuer}:{account}"
    params = {
        "secret": secret_b32,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": str(_DIGITS),
        "period": str(_STEP_S),
    }
    qs = "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())
    return f"otpauth://totp/{quote(label, safe=':')}?{qs}"
