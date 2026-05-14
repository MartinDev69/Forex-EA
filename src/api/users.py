"""User + AD-ID storage for the control API.

All identities are AD-IDs:
- `Admi8X` is the singleton admin — protected from deletion, demotion, and duplication.
- Regular operators receive random AD-IDs (`AD-XXXXXXXX`) drawn from a
  pre-generated pool of 100 unclaimed IDs. The admin assigns an ID + email
  with a subscription duration; an emailed setup link lets the operator
  choose their own password. Subscriptions expire — login is rejected
  past expiry and the user is emailed asking them to contact admin to renew.

Storage layout (one SQLite file shared with trades + toggles):

  auth_users (ad_id PK, password_hash NULLABLE, role, email, created_at,
              expires_at NULLABLE, expired_notified_at NULLABLE)
  ad_id_pool (ad_id PK, generated_at)            — unclaimed IDs only
  used_setup_tokens (jti PK, used_at)            — single-use link tracking
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .ad_id import ADMIN_AD_ID, new_ad_id

ROLES = ("admin", "user")
DEFAULT_POOL_SIZE = 100

# Human-friendly duration codes the dashboard uses. Keep keys stable —
# the UI hard-codes them. Values are timedelta-equivalents.
SUBSCRIPTION_DURATIONS: dict[str, timedelta] = {
    "5h":  timedelta(hours=5),
    "1w":  timedelta(weeks=1),
    "2w":  timedelta(weeks=2),
    "1m":  timedelta(days=30),
    "2m":  timedelta(days=60),
    "3m":  timedelta(days=90),
}


def parse_duration(code: str) -> timedelta:
    """Resolve a duration code (e.g. '2w') to a timedelta. Raises
    ValueError for anything not in SUBSCRIPTION_DURATIONS — the API
    surfaces this as a 400 to the admin.
    """
    td = SUBSCRIPTION_DURATIONS.get(code)
    if td is None:
        raise ValueError(
            f"unknown subscription duration {code!r}; "
            f"choose one of {list(SUBSCRIPTION_DURATIONS)}"
        )
    return td

SCHEMA_USERS = """
CREATE TABLE IF NOT EXISTS auth_users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT,
    role          TEXT NOT NULL DEFAULT 'user',
    email         TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

SCHEMA_POOL = """
CREATE TABLE IF NOT EXISTS ad_id_pool (
    ad_id        TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

SCHEMA_USED_TOKENS = """
CREATE TABLE IF NOT EXISTS used_setup_tokens (
    jti     TEXT PRIMARY KEY,
    used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# Per-operator strategy picks captured during Telegram signup. Each
# user picks 3 strategies for signal alerts and 2 for auto-execute;
# these are locked for the subscription term. The bot's per-trade
# fan-out and the /signals/feed copier consult this table at
# delivery time to decide who hears about each strategy's activity.
SCHEMA_USER_PICKS = """
CREATE TABLE IF NOT EXISTS user_strategy_picks (
    username      TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    kind          TEXT NOT NULL,   -- 'signal' | 'execute'
    picked_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (username, strategy, kind)
)
"""


@dataclass(frozen=True)
class UserRecord:
    username: str
    role: str
    email: str | None
    created_at: str
    # True when the user has chosen a password and can log in. Assigned-but-
    # not-yet-set-up operators have password_set=False.
    password_set: bool
    # ISO timestamp; None means never expires (admin accounts get None).
    expires_at: str | None = None
    # True if expires_at is in the past. Computed at read-time.
    expired: bool = False
    # Phone number (from Telegram contact share). Optional.
    phone_number: str | None = None
    # Friendly display name (typically the Telegram first_name). Optional.
    display_name: str | None = None


class LastAdminError(RuntimeError):
    """Raised when an action would leave the system with zero admins."""


class DuplicateAdminError(RuntimeError):
    """Raised when trying to create or promote a second admin."""


class UserStore:
    def __init__(self, db_path: Path | str, *, pool_size: int = DEFAULT_POOL_SIZE) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(SCHEMA_USERS)
            c.execute(SCHEMA_POOL)
            c.execute(SCHEMA_USED_TOKENS)
            c.execute(SCHEMA_USER_PICKS)
            self._migrate(c)
        # Keep the pool topped up. Safe to call on every boot — no-op if full.
        self.refill_pool(pool_size)

    def _migrate(self, c: sqlite3.Connection) -> None:
        """Walk DBs built against older schemas forward to the current shape."""
        cols = {r[1] for r in c.execute("PRAGMA table_info(auth_users)").fetchall()}
        if "role" not in cols:
            c.execute("ALTER TABLE auth_users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        if "email" not in cols:
            c.execute("ALTER TABLE auth_users ADD COLUMN email TEXT")
        if "expires_at" not in cols:
            c.execute("ALTER TABLE auth_users ADD COLUMN expires_at TEXT")
        if "expired_notified_at" not in cols:
            c.execute("ALTER TABLE auth_users ADD COLUMN expired_notified_at TEXT")
        if "phone_number" not in cols:
            c.execute("ALTER TABLE auth_users ADD COLUMN phone_number TEXT")
        if "display_name" not in cols:
            c.execute("ALTER TABLE auth_users ADD COLUMN display_name TEXT")
        # Long-lived API key for the user's MetaTrader copy-trading EA.
        # Issued once when the user activates their password; the EA
        # passes it as Authorization: Bearer <key>.
        if "ea_api_key" not in cols:
            c.execute("ALTER TABLE auth_users ADD COLUMN ea_api_key TEXT")
        # Telegram chat ID, captured when their signup request was
        # approved. Bot uses this to fan out signal/execution alerts
        # to each operator (filtered by user-copyable strategies).
        if "telegram_chat_id" not in cols:
            c.execute("ALTER TABLE auth_users ADD COLUMN telegram_chat_id INTEGER")
        # password_hash was NOT NULL pre-setup-flow; make it nullable so we can
        # pre-seed assigned users before they've chosen a password. SQLite
        # can't ALTER constraints in place, so we migrate via table rebuild
        # only if the current column is NOT NULL.
        notnull = {r[1]: r[3] for r in c.execute("PRAGMA table_info(auth_users)").fetchall()}
        if notnull.get("password_hash") == 1:
            c.executescript("""
                CREATE TABLE auth_users_new (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT,
                    role TEXT NOT NULL DEFAULT 'user',
                    email TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO auth_users_new (username, password_hash, role, email, created_at)
                SELECT username, password_hash, role, email, created_at FROM auth_users;
                DROP TABLE auth_users;
                ALTER TABLE auth_users_new RENAME TO auth_users;
            """)
        # Role rename: legacy 'viewer' accounts become 'user'.
        c.execute("UPDATE auth_users SET role = 'user' WHERE role = 'viewer'")
        # Admin migration: if there's exactly one admin and they aren't Admi8X
        # yet, rename them so they can keep logging in with their existing
        # password under the new identity.
        admins = c.execute(
            "SELECT username FROM auth_users WHERE role = 'admin'"
        ).fetchall()
        names = [a[0] for a in admins]
        if len(names) == 1 and names[0] != ADMIN_AD_ID:
            # If the target name is somehow already taken by a non-admin row,
            # leave things alone and surface via logs — human must intervene.
            clash = c.execute(
                "SELECT 1 FROM auth_users WHERE username = ?", (ADMIN_AD_ID,)
            ).fetchone()
            if clash is None:
                c.execute(
                    "UPDATE auth_users SET username = ? WHERE username = ?",
                    (ADMIN_AD_ID, names[0]),
                )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ---------- Pool ----------

    def refill_pool(self, target: int = DEFAULT_POOL_SIZE) -> int:
        """Ensure the pool has at least `target` unclaimed IDs. Returns new count added."""
        with self._conn() as c:
            have = c.execute("SELECT COUNT(*) FROM ad_id_pool").fetchone()[0]
            added = 0
            while have + added < target:
                candidate = new_ad_id()
                # Skip if this ID is already in the pool or already a user.
                exists_pool = c.execute(
                    "SELECT 1 FROM ad_id_pool WHERE ad_id = ?", (candidate,)
                ).fetchone()
                exists_user = c.execute(
                    "SELECT 1 FROM auth_users WHERE username = ?", (candidate,)
                ).fetchone()
                if exists_pool or exists_user:
                    continue
                c.execute("INSERT INTO ad_id_pool (ad_id) VALUES (?)", (candidate,))
                added += 1
            return added

    def unclaimed_pool(self) -> list[str]:
        with self._conn() as c:
            rows = c.execute("SELECT ad_id FROM ad_id_pool ORDER BY generated_at").fetchall()
        return [r["ad_id"] for r in rows]

    def pool_size(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM ad_id_pool").fetchone()[0])

    def claim_ad_id(self, ad_id: str) -> bool:
        """Remove an ID from the pool. Returns True if it was present."""
        with self._conn() as c:
            cur = c.execute("DELETE FROM ad_id_pool WHERE ad_id = ?", (ad_id,))
        return cur.rowcount > 0

    # ---------- Users ----------

    @staticmethod
    def _check_role(role: str) -> None:
        if role not in ROLES:
            raise ValueError(f"invalid role {role!r}, must be one of {ROLES}")

    def _admin_count(self, c: sqlite3.Connection) -> int:
        return int(c.execute(
            "SELECT COUNT(*) FROM auth_users WHERE role = 'admin'"
        ).fetchone()[0])

    def assign(
        self, ad_id: str, email: str, *,
        duration: timedelta | None = None,
        now: datetime | None = None,
        phone_number: str | None = None,
        display_name: str | None = None,
    ) -> str | None:
        """Seed a user-role row with no password yet (pending setup).

        Claims the AD-ID from the pool. Rejects the singleton admin ID — the
        admin is seeded via scripts/create_user.py, not the assign flow.

        ``duration`` sets the subscription window; expires_at = now + duration.
        Pass None for an unlimited subscription (kept for backwards compat
        with older callers / the admin account). Returns the ISO expires_at
        the row was stamped with (or None if no duration).
        """
        if ad_id == ADMIN_AD_ID:
            raise ValueError(f"{ADMIN_AD_ID} is reserved for the admin")
        now = now or datetime.now(timezone.utc)
        expires_at_iso = (now + duration).isoformat() if duration is not None else None
        with self._conn() as c:
            # The AD-ID must come from the pool so admin can't mint arbitrary IDs.
            if not c.execute(
                "SELECT 1 FROM ad_id_pool WHERE ad_id = ?", (ad_id,)
            ).fetchone():
                raise ValueError(f"{ad_id} is not in the unclaimed pool")
            c.execute("DELETE FROM ad_id_pool WHERE ad_id = ?", (ad_id,))
            # On re-assign (e.g. the admin wants to re-email the setup link),
            # overwrite the email but only if the row has no password yet —
            # otherwise we'd silently wipe a real user.
            existing = c.execute(
                "SELECT password_hash FROM auth_users WHERE username = ?", (ad_id,)
            ).fetchone()
            if existing is None:
                c.execute(
                    "INSERT INTO auth_users (username, password_hash, role, "
                    "email, expires_at, phone_number, display_name) "
                    "VALUES (?, NULL, 'user', ?, ?, ?, ?)",
                    (ad_id, email, expires_at_iso, phone_number, display_name),
                )
            elif existing["password_hash"] is None:
                c.execute(
                    "UPDATE auth_users SET email = ?, expires_at = ?, "
                    "expired_notified_at = NULL, "
                    "phone_number = COALESCE(?, phone_number), "
                    "display_name = COALESCE(?, display_name) "
                    "WHERE username = ?",
                    (email, expires_at_iso, phone_number, display_name, ad_id),
                )
            else:
                raise ValueError(f"{ad_id} already has a password set")
        return expires_at_iso

    def extend(
        self, username: str, duration: timedelta, *,
        now: datetime | None = None,
    ) -> str | None:
        """Push the user's expires_at forward by ``duration``. If the
        subscription already lapsed, anchors the extension at now (so a
        2-week renewal of a long-expired user gives 2 weeks from now,
        not 2 weeks from the past expiry). Returns the new ISO expires_at.

        Clears the expired-notified flag so a future expiry triggers a
        fresh email rather than being suppressed by the previous one.
        """
        if username == ADMIN_AD_ID:
            raise ValueError(f"{ADMIN_AD_ID} has no subscription to extend")
        now = now or datetime.now(timezone.utc)
        with self._conn() as c:
            row = c.execute(
                "SELECT expires_at FROM auth_users WHERE username = ?",
                (username,),
            ).fetchone()
            if row is None:
                raise KeyError(username)
            current = row["expires_at"]
            try:
                anchor = datetime.fromisoformat(current) if current else now
            except ValueError:
                anchor = now
            # If the user already lapsed, restart from now rather than
            # tacking onto a past expiry.
            if anchor < now:
                anchor = now
            new_expires = (anchor + duration).isoformat()
            c.execute(
                "UPDATE auth_users SET expires_at = ?, "
                "expired_notified_at = NULL WHERE username = ?",
                (new_expires, username),
            )
        return new_expires

    def get_expires_at(self, username: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT expires_at FROM auth_users WHERE username = ?",
                (username,),
            ).fetchone()
        return row["expires_at"] if row else None

    def is_expired(self, username: str, *, now: datetime | None = None) -> bool:
        """True if the user has an expires_at in the past. Admin and
        users with no expires_at always return False.
        """
        if username == ADMIN_AD_ID:
            return False
        ts = self.get_expires_at(username)
        if not ts:
            return False
        try:
            expires = datetime.fromisoformat(ts)
        except ValueError:
            return False
        now = now or datetime.now(timezone.utc)
        return expires < now

    def list_expired_unnotified(self, *, now: datetime | None = None) -> list[UserRecord]:
        """Users whose expires_at has just passed and we haven't yet
        emailed the "subscription expired" notice for. The expiry-email
        cron pulls this list, sends each one, then calls mark_notified.
        """
        now = now or datetime.now(timezone.utc)
        with self._conn() as c:
            rows = c.execute(
                "SELECT username, role, email, created_at, password_hash, "
                "expires_at, expired_notified_at FROM auth_users "
                "WHERE expires_at IS NOT NULL "
                "  AND expires_at < ? "
                "  AND expired_notified_at IS NULL "
                "  AND username != ?",
                (now.isoformat(), ADMIN_AD_ID),
            ).fetchall()
        return [
            UserRecord(
                username=r["username"], role=r["role"], email=r["email"],
                created_at=r["created_at"],
                password_set=bool(r["password_hash"]),
                expires_at=r["expires_at"],
                expired=True,
            )
            for r in rows
        ]

    def mark_notified(self, username: str, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        with self._conn() as c:
            c.execute(
                "UPDATE auth_users SET expired_notified_at = ? WHERE username = ?",
                (now.isoformat(), username),
            )

    def create_admin(self, password_hash: str) -> None:
        """Seed the singleton admin. Idempotent only when the admin doesn't exist yet."""
        with self._conn() as c:
            if self._admin_count(c) >= 1:
                raise DuplicateAdminError("an admin already exists")
            c.execute(
                "INSERT INTO auth_users (username, password_hash, role) VALUES (?, ?, 'admin')",
                (ADMIN_AD_ID, password_hash),
            )

    def create(self, username: str, password_hash: str, role: str = "user") -> None:
        """Low-level insert used by seed scripts and tests.

        Enforces the singleton-admin invariant: admin role is only valid for
        `ADMIN_AD_ID`, and only when no admin exists. The admin-facing flow uses
        `assign()` + setup link rather than this method.
        """
        self._check_role(role)
        with self._conn() as c:
            if role == "admin":
                if username != ADMIN_AD_ID:
                    raise ValueError(f"admin username must be {ADMIN_AD_ID}")
                if self._admin_count(c) >= 1:
                    raise DuplicateAdminError("an admin already exists")
            c.execute(
                "INSERT INTO auth_users (username, password_hash, role) VALUES (?, ?, ?)",
                (username, password_hash, role),
            )

    def update_password(self, username: str, new_hash: str) -> bool:
        """Alias for set_password — kept so older callers read naturally."""
        return self.set_password(username, new_hash)

    def set_password(self, username: str, new_hash: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE auth_users SET password_hash = ? WHERE username = ?",
                (new_hash, username),
            )
        return cur.rowcount > 0

    def set_telegram_chat_id(self, username: str, chat_id: int) -> None:
        """Persist the operator's Telegram chat id so the bot can fan
        signal/execution alerts out to them."""
        with self._conn() as c:
            c.execute(
                "UPDATE auth_users SET telegram_chat_id = ? WHERE username = ?",
                (int(chat_id), username),
            )

    def list_active_telegram_chats(
        self, *, now: datetime | None = None,
    ) -> list[tuple[str, int]]:
        """Return (username, telegram_chat_id) for every operator who:
          - has a chat id stored,
          - is not the admin,
          - has a password set (subscription is active, not pending),
          - has not had their subscription expire.
        Used by the bot's Telegram fan-out.
        """
        now = now or datetime.now(timezone.utc)
        with self._conn() as c:
            rows = c.execute(
                "SELECT username, telegram_chat_id, expires_at FROM auth_users "
                "WHERE username != ? "
                "  AND telegram_chat_id IS NOT NULL "
                "  AND password_hash IS NOT NULL",
                (ADMIN_AD_ID,),
            ).fetchall()
        out: list[tuple[str, int]] = []
        for r in rows:
            ts = r["expires_at"]
            if ts:
                try:
                    if datetime.fromisoformat(ts) < now:
                        continue  # expired — skip
                except ValueError:
                    pass
            out.append((r["username"], int(r["telegram_chat_id"])))
        return out

    # ---------- per-user strategy picks ----------

    def set_user_picks(
        self,
        username: str,
        *,
        signal: list[str],
        execute: list[str],
    ) -> None:
        """Replace this user's strategy picks for the subscription term.
        Caller is responsible for validating counts (3 signal, 2 execute)
        — the store just persists what's given.
        """
        with self._conn() as c:
            c.execute(
                "DELETE FROM user_strategy_picks WHERE username = ?", (username,),
            )
            for s in signal:
                c.execute(
                    "INSERT OR IGNORE INTO user_strategy_picks "
                    "(username, strategy, kind) VALUES (?, ?, 'signal')",
                    (username, s),
                )
            for s in execute:
                c.execute(
                    "INSERT OR IGNORE INTO user_strategy_picks "
                    "(username, strategy, kind) VALUES (?, ?, 'execute')",
                    (username, s),
                )

    def get_user_picks(self, username: str) -> dict[str, set[str]]:
        """Return {'signal': set[strategy], 'execute': set[strategy]} for
        this user. Empty sets if none picked.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT strategy, kind FROM user_strategy_picks WHERE username = ?",
                (username,),
            ).fetchall()
        out: dict[str, set[str]] = {"signal": set(), "execute": set()}
        for r in rows:
            kind = r["kind"]
            if kind in out:
                out[kind].add(r["strategy"])
        return out

    def list_users_who_picked(
        self, strategy: str, kind: str,
    ) -> list[tuple[str, int | None]]:
        """Return (username, telegram_chat_id) for every user who picked
        this (strategy, kind). Used by the bot's per-user fan-out.
        Filters out unauthenticated rows (no password set yet) — those
        are pending signups, not active operators.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT p.username AS username, u.telegram_chat_id AS chat "
                "FROM user_strategy_picks p "
                "JOIN auth_users u ON u.username = p.username "
                "WHERE p.strategy = ? AND p.kind = ? "
                "  AND u.password_hash IS NOT NULL",
                (strategy, kind),
            ).fetchall()
        return [
            (r["username"], int(r["chat"]) if r["chat"] is not None else None)
            for r in rows
        ]

    def ensure_ea_api_key(self, username: str) -> str:
        """Return the user's EA API key; generates one on first call.

        Idempotent — once a key is issued, subsequent calls return the
        same key (don't regenerate, since the user's installed EA
        already has the old one configured).
        """
        import secrets
        with self._conn() as c:
            row = c.execute(
                "SELECT ea_api_key FROM auth_users WHERE username = ?",
                (username,),
            ).fetchone()
            if row is None:
                raise KeyError(username)
            existing = row["ea_api_key"]
            if existing:
                return existing
            new_key = "ea_" + secrets.token_urlsafe(32)
            c.execute(
                "UPDATE auth_users SET ea_api_key = ? WHERE username = ?",
                (new_key, username),
            )
            return new_key

    def rotate_ea_api_key(self, username: str) -> str:
        """Force-issue a new EA key, invalidating any existing one.
        User's currently-installed EA stops working until they re-paste
        the new key — useful after a leak.
        """
        import secrets
        new_key = "ea_" + secrets.token_urlsafe(32)
        with self._conn() as c:
            cur = c.execute(
                "UPDATE auth_users SET ea_api_key = ? WHERE username = ?",
                (new_key, username),
            )
            if cur.rowcount == 0:
                raise KeyError(username)
        return new_key

    def get_username_by_ea_key(self, key: str) -> str | None:
        """Reverse lookup for the EA's bearer token. Returns None if
        the key isn't recognised.
        """
        if not key or not key.startswith("ea_"):
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT username FROM auth_users WHERE ea_api_key = ?", (key,),
            ).fetchone()
        return row["username"] if row else None

    def get_hash(self, username: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT password_hash FROM auth_users WHERE username = ?",
                (username,),
            ).fetchone()
        return row["password_hash"] if row and row["password_hash"] else None

    def get_role(self, username: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT role FROM auth_users WHERE username = ?",
                (username,),
            ).fetchone()
        return row["role"] if row else None

    def get_email(self, username: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT email FROM auth_users WHERE username = ?",
                (username,),
            ).fetchone()
        return row["email"] if row else None

    def exists(self, username: str) -> bool:
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM auth_users WHERE username = ?", (username,)
            ).fetchone() is not None

    def list_users(self) -> list[UserRecord]:
        now = datetime.now(timezone.utc)
        with self._conn() as c:
            rows = c.execute(
                "SELECT username, role, email, created_at, password_hash, "
                "expires_at, phone_number, display_name "
                "FROM auth_users ORDER BY role DESC, username"
            ).fetchall()
        out: list[UserRecord] = []
        for r in rows:
            expired = False
            if r["expires_at"] and r["username"] != ADMIN_AD_ID:
                try:
                    expired = datetime.fromisoformat(r["expires_at"]) < now
                except ValueError:
                    expired = False
            out.append(UserRecord(
                username=r["username"], role=r["role"], email=r["email"],
                created_at=r["created_at"],
                password_set=bool(r["password_hash"]),
                expires_at=r["expires_at"],
                expired=expired,
                phone_number=r["phone_number"] if "phone_number" in r.keys() else None,
                display_name=r["display_name"] if "display_name" in r.keys() else None,
            ))
        return out

    def list_usernames(self) -> list[str]:
        return [u.username for u in self.list_users()]

    def delete(self, username: str) -> bool:
        if username == ADMIN_AD_ID:
            raise LastAdminError(f"{ADMIN_AD_ID} is protected and cannot be deleted")
        with self._conn() as c:
            cur = c.execute("DELETE FROM auth_users WHERE username = ?", (username,))
        return cur.rowcount > 0

    # ---------- Setup tokens (single-use tracking) ----------

    def mark_token_used(self, jti: str) -> bool:
        """Returns True if this was the first time; False if already burned."""
        with self._conn() as c:
            try:
                c.execute("INSERT INTO used_setup_tokens (jti) VALUES (?)", (jti,))
                return True
            except sqlite3.IntegrityError:
                return False

    def token_was_used(self, jti: str) -> bool:
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM used_setup_tokens WHERE jti = ?", (jti,)
            ).fetchone() is not None
