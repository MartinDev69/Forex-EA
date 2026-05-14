"""Pending subscription requests collected by the Telegram signup bot.

A user DMs the bot, picks a duration, supplies their email — that
combination becomes a row here with status='pending'. The admin sees
pending rows in the dashboard and can approve (which assigns an AD-ID
and emails the setup link) or reject (with a reason).

Per-chat conversation state is also stored here so the polling loop
is stateless across restarts — the bot can crash mid-conversation
and the user keeps the same flow when it comes back.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Allowed durations — same codes as the operator-assign API.
VALID_DURATIONS = ("5h", "1w", "2w", "1m", "2m", "3m")
VALID_STATUSES = ("pending", "approved", "rejected")

# Conversation states the signup bot walks each chat through.
STATE_IDLE                = "idle"
STATE_AWAITING_EMAIL      = "awaiting_email"
STATE_AWAITING_PHONE      = "awaiting_phone"
STATE_PICKING_SIGNALS     = "picking_signals"
STATE_PICKING_EXECUTE     = "picking_execute"

# How many strategies each kind requires before the bot accepts the
# selection and moves on.
PICKS_REQUIRED = {"signal": 3, "execute": 2}


def _split_csv(value: str | None) -> list[str]:
    """Parse a comma-separated picks string back into a list. Tolerates
    None, empty, and stray whitespace.
    """
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]

_REQUESTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscription_requests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_chat_id    INTEGER NOT NULL,
    telegram_username   TEXT,
    telegram_first_name TEXT,
    duration            TEXT NOT NULL,
    email               TEXT NOT NULL DEFAULT '',
    phone_number        TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',
    created_at          TEXT NOT NULL,
    decided_at          TEXT,
    decided_by          TEXT,
    assigned_ad_id      TEXT,
    rejection_reason    TEXT,
    -- Per-user strategy picks captured during signup. Stored as
    -- comma-separated strategy names (joined here, exploded on read
    -- via .split). Transferred to user_strategy_picks on approval.
    picks_signal        TEXT NOT NULL DEFAULT '',
    picks_execute       TEXT NOT NULL DEFAULT ''
);
"""

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_chat_state (
    chat_id           INTEGER PRIMARY KEY,
    telegram_username TEXT,
    first_name        TEXT,
    state             TEXT NOT NULL DEFAULT 'idle',
    duration          TEXT,
    updated_at        TEXT NOT NULL,
    -- In-progress strategy picks while the user is still tapping the
    -- inline checkboxes. Comma-separated names. Cleared when the
    -- signup request is finalised.
    picks_signal      TEXT NOT NULL DEFAULT '',
    picks_execute     TEXT NOT NULL DEFAULT ''
);
"""

# Long-poll bookkeeping — Telegram Bot API requires us to track the
# last update_id we processed so we can ack it on the next call.
_OFFSET_SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_signup_offset (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    update_id    INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class SubscriptionRequest:
    id: int
    telegram_chat_id: int
    telegram_username: str | None
    telegram_first_name: str | None
    duration: str
    email: str
    phone_number: str | None
    status: str
    created_at: str
    decided_at: str | None
    decided_by: str | None
    assigned_ad_id: str | None
    rejection_reason: str | None
    picks_signal: list[str] = field(default_factory=list)
    picks_execute: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ChatState:
    chat_id: int
    telegram_username: str | None
    first_name: str | None
    state: str
    duration: str | None
    updated_at: str
    picks_signal: list[str] = field(default_factory=list)
    picks_execute: list[str] = field(default_factory=list)


class SubscriptionRequestStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(_REQUESTS_SCHEMA)
            c.execute(_STATE_SCHEMA)
            c.execute(_OFFSET_SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(subscription_requests)")}
            if "phone_number" not in cols:
                c.execute("ALTER TABLE subscription_requests ADD COLUMN phone_number TEXT")
            # Additive: per-user strategy picks captured during signup.
            if "picks_signal" not in cols:
                c.execute(
                    "ALTER TABLE subscription_requests "
                    "ADD COLUMN picks_signal TEXT NOT NULL DEFAULT ''"
                )
            if "picks_execute" not in cols:
                c.execute(
                    "ALTER TABLE subscription_requests "
                    "ADD COLUMN picks_execute TEXT NOT NULL DEFAULT ''"
                )
            # Additive: in-progress picks on the chat-state side.
            state_cols = {r["name"] for r in c.execute("PRAGMA table_info(telegram_chat_state)")}
            if "picks_signal" not in state_cols:
                c.execute(
                    "ALTER TABLE telegram_chat_state "
                    "ADD COLUMN picks_signal TEXT NOT NULL DEFAULT ''"
                )
            if "picks_execute" not in state_cols:
                c.execute(
                    "ALTER TABLE telegram_chat_state "
                    "ADD COLUMN picks_execute TEXT NOT NULL DEFAULT ''"
                )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------- conversation state ----------

    def get_state(self, chat_id: int) -> ChatState | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT chat_id, telegram_username, first_name, state, duration, "
                "       updated_at, picks_signal, picks_execute "
                "FROM telegram_chat_state WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        return ChatState(
            chat_id=row["chat_id"],
            telegram_username=row["telegram_username"],
            first_name=row["first_name"],
            state=row["state"],
            duration=row["duration"],
            updated_at=row["updated_at"],
            picks_signal=_split_csv(row["picks_signal"]),
            picks_execute=_split_csv(row["picks_execute"]),
        )

    def set_state_picks(
        self, chat_id: int, *,
        signal: list[str] | None = None,
        execute: list[str] | None = None,
    ) -> None:
        """Replace the in-progress picks lists on the chat-state row.
        Either argument None means "leave this kind alone". Counts are
        not validated here — the caller (the picker UI) enforces them.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            sets = ["updated_at = ?"]
            args: list[object] = [now]
            if signal is not None:
                sets.append("picks_signal = ?")
                args.append(",".join(signal))
            if execute is not None:
                sets.append("picks_execute = ?")
                args.append(",".join(execute))
            args.append(chat_id)
            c.execute(
                "UPDATE telegram_chat_state SET " + ", ".join(sets) +
                " WHERE chat_id = ?",
                args,
            )

    def upsert_state(
        self, chat_id: int, *, state: str,
        duration: str | None = None,
        username: str | None = None,
        first_name: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO telegram_chat_state
                  (chat_id, telegram_username, first_name, state, duration, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  state = excluded.state,
                  duration = excluded.duration,
                  telegram_username = COALESCE(excluded.telegram_username, telegram_chat_state.telegram_username),
                  first_name = COALESCE(excluded.first_name, telegram_chat_state.first_name),
                  updated_at = excluded.updated_at
                """,
                (chat_id, username, first_name, state, duration, now),
            )

    # ---------- subscription requests ----------

    def create_request(
        self, *, chat_id: int, username: str | None, first_name: str | None,
        duration: str, email: str = "", phone_number: str | None = None,
        picks_signal: list[str] | None = None,
        picks_execute: list[str] | None = None,
    ) -> int:
        if duration not in VALID_DURATIONS:
            raise ValueError(f"invalid duration: {duration}")
        now = datetime.now(timezone.utc).isoformat()
        sig = ",".join(picks_signal or [])
        exe = ",".join(picks_execute or [])
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO subscription_requests
                  (telegram_chat_id, telegram_username, telegram_first_name,
                   duration, email, phone_number, status, created_at,
                   picks_signal, picks_execute)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (chat_id, username, first_name, duration, email, phone_number,
                 now, sig, exe),
            )
            return int(cur.lastrowid)

    def list_pending(self) -> list[SubscriptionRequest]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM subscription_requests "
                "WHERE status = 'pending' ORDER BY created_at"
            ).fetchall()
        return [self._row_to_request(r) for r in rows]

    def list_recent(self, limit: int = 50) -> list[SubscriptionRequest]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM subscription_requests "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_request(r) for r in rows]

    def get(self, request_id: int) -> SubscriptionRequest | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM subscription_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
        return self._row_to_request(row) if row else None

    def mark_approved(
        self, request_id: int, *, admin: str, assigned_ad_id: str,
    ) -> SubscriptionRequest | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                """
                UPDATE subscription_requests
                SET status = 'approved', decided_at = ?, decided_by = ?,
                    assigned_ad_id = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, admin, assigned_ad_id, request_id),
            )
        return self.get(request_id)

    def mark_rejected(
        self, request_id: int, *, admin: str, reason: str,
    ) -> SubscriptionRequest | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                """
                UPDATE subscription_requests
                SET status = 'rejected', decided_at = ?, decided_by = ?,
                    rejection_reason = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, admin, reason, request_id),
            )
        return self.get(request_id)

    @staticmethod
    def _row_to_request(row: sqlite3.Row) -> SubscriptionRequest:
        keys = row.keys()
        return SubscriptionRequest(
            id=row["id"],
            telegram_chat_id=row["telegram_chat_id"],
            telegram_username=row["telegram_username"],
            telegram_first_name=row["telegram_first_name"],
            duration=row["duration"],
            email=row["email"] or "",
            phone_number=(row["phone_number"] if "phone_number" in keys else None),
            status=row["status"],
            created_at=row["created_at"],
            decided_at=row["decided_at"],
            decided_by=row["decided_by"],
            assigned_ad_id=row["assigned_ad_id"],
            rejection_reason=row["rejection_reason"],
            picks_signal=_split_csv(row["picks_signal"]) if "picks_signal" in keys else [],
            picks_execute=_split_csv(row["picks_execute"]) if "picks_execute" in keys else [],
        )

    # ---------- update offset ----------

    def get_update_offset(self) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT update_id FROM telegram_signup_offset WHERE id = 1"
            ).fetchone()
        return int(row["update_id"]) if row else 0

    def set_update_offset(self, update_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO telegram_signup_offset (id, update_id, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  update_id = excluded.update_id,
                  updated_at = excluded.updated_at
                """,
                (update_id, now),
            )
