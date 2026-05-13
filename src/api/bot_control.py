"""Cross-process bot start/stop control surfaced via SQLite.

The API and bot run in separate NSSM services and don't share memory, so
``state.running`` set in the API process never reaches the bot. This
store is a single-row latch both processes read/write: the API writes
when the operator clicks Start/Stop on the dashboard, the bot polls it
at the top of each tick and pauses (without exiting) when ``should_run``
is False.

Modelled on the voice KillSwitchFlag pattern, but kept separate because
the semantics differ — kill switch is one-shot/emergency, this is a
normal lifecycle pause that operators flip back and forth.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_control (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    should_run  INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT
);
"""


@dataclass(frozen=True)
class BotControlState:
    should_run: bool
    updated_at: datetime | None
    updated_by: str | None


class BotControlStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            # Seed the row if missing — defaults to should_run=True so an
            # uninitialised install behaves like the old in-memory flag.
            c.execute(
                "INSERT OR IGNORE INTO bot_control (id, should_run, updated_at, updated_by) "
                "VALUES (1, 1, ?, NULL)",
                (datetime.now(timezone.utc).isoformat(),),
            )

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def should_run(self) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT should_run FROM bot_control WHERE id = 1"
            ).fetchone()
        return bool(row["should_run"]) if row else True

    def set(self, value: bool, *, by: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                "INSERT INTO bot_control (id, should_run, updated_at, updated_by) "
                "VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "  should_run = excluded.should_run, "
                "  updated_at = excluded.updated_at, "
                "  updated_by = excluded.updated_by",
                (1 if value else 0, now, by),
            )

    def state(self) -> BotControlState:
        with self._conn() as c:
            row = c.execute(
                "SELECT should_run, updated_at, updated_by "
                "FROM bot_control WHERE id = 1"
            ).fetchone()
        if row is None:
            return BotControlState(True, None, None)
        return BotControlState(
            should_run=bool(row["should_run"]),
            updated_at=(
                datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None
            ),
            updated_by=row["updated_by"],
        )
