"""Seed or reset the singleton admin for the control API.

The admin identity is fixed to `Admi8X` — it's the only account created via
this script. Regular operators are added from the admin dashboard through the
assign-by-email flow (pool AD-ID + setup link) and never go through here.

Usage:
  python scripts/create_user.py
    # prompts for password; creates the Admi8X admin

  python scripts/create_user.py --password-stdin
    # reads password from stdin (for automation)

  python scripts/create_user.py --reset
    # resets the Admi8X password instead of failing on duplicate

  python scripts/create_user.py --username AD-AB12CD34 --reset
    # resets password for any existing user (useful if setup link was lost
    # and email is unavailable)
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

# Allow `python scripts/create_user.py ...` from the repo root without needing
# PYTHONPATH — Python only puts the script's own dir on sys.path by default.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.ad_id import ADMIN_AD_ID  # noqa: E402
from src.api.auth import hash_password  # noqa: E402
from src.api.users import DuplicateAdminError, UserStore  # noqa: E402


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        pw = sys.stdin.readline().rstrip("\n")
        if not pw:
            raise SystemExit("empty password on stdin")
        return pw
    pw1 = getpass.getpass("Password: ")
    pw2 = getpass.getpass("Confirm:  ")
    if pw1 != pw2:
        raise SystemExit("passwords do not match")
    if len(pw1) < 12:
        raise SystemExit("password must be at least 12 characters")
    return pw1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/trades.db", type=Path)
    p.add_argument("--username", default=ADMIN_AD_ID,
                   help=f"account to operate on (default: {ADMIN_AD_ID})")
    p.add_argument("--password-stdin", action="store_true",
                   help="read password from stdin instead of prompting")
    p.add_argument("--reset", action="store_true",
                   help="reset the account's password instead of creating it")
    args = p.parse_args(argv)

    pw = _read_password(args.password_stdin)
    store = UserStore(args.db)
    h = hash_password(pw)

    if args.reset:
        if not store.exists(args.username):
            print(f"user {args.username!r} not found", file=sys.stderr)
            return 2
        store.set_password(args.username, h)
        print(f"password updated for {args.username}")
        return 0

    if args.username != ADMIN_AD_ID:
        print(
            f"refusing to create {args.username!r}: only the {ADMIN_AD_ID} admin "
            "is seeded from the CLI. Use the dashboard's assign flow for operators.",
            file=sys.stderr,
        )
        return 2
    try:
        store.create_admin(h)
    except DuplicateAdminError:
        print(
            f"admin already exists — rerun with --reset to change the password.",
            file=sys.stderr,
        )
        return 2
    print(f"admin {ADMIN_AD_ID} created in {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
