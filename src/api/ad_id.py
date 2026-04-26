"""AD-ID: the login identifier for bot operators.

The admin has a fixed, protected AD-ID (`Admi8X`) — only one admin exists
ever. Regular users receive random AD-IDs drawn from a pre-generated pool
(`AD-XXXXXXXX`, 8 hex chars). The admin assigns an ID + email; the server
emails a one-time setup link so the recipient can pick their own password.
"""
from __future__ import annotations

import re
import secrets

# The sole admin identity. Protected: can't be demoted, deleted, or duplicated.
ADMIN_AD_ID = "Admi8X"

_USER_AD_ID_RE = re.compile(r"^AD-[0-9A-F]{8}$")


def new_ad_id() -> str:
    """Return a fresh user-role AD-ID. Callers check the pool for collisions."""
    return f"AD-{secrets.token_hex(4).upper()}"


def is_user_ad_id(ad_id: str) -> bool:
    return bool(_USER_AD_ID_RE.match(ad_id))


def is_admin_ad_id(ad_id: str) -> bool:
    return ad_id == ADMIN_AD_ID
