"""Per-process heartbeat store.

The bot calls `write()` once per tick. The watchdog calls `read()` to decide
whether the bot is making progress. Single SQLite UPSERT — negligible cost.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchdog_heartbeat (
    process_name  TEXT PRIMARY KEY,
    last_tick_at  TEXT NOT NULL,
    tick_count    INTEGER NOT NULL,
    pid           INTEGER,
    last_error    TEXT
);
"""


@dataclass
class Heartbeat:
    process_name: str
    last_tick_at: datetime
    tick_count: int
    pid: int | None
    last_error: str | None

    def age_seconds(self, now: datetime | None = None) -> float:
        ref = now or datetime.now(timezone.utc)
        return (ref - self.last_tick_at).total_seconds()


class HeartbeatStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def write(
        self,
        *,
        process_name: str,
        tick_count: int,
        last_error: str | None = None,
        pid: int | None = None,
        now: datetime | None = None,
    ) -> None:
        ts = (now or datetime.now(timezone.utc)).isoformat()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO watchdog_heartbeat
                    (process_name, last_tick_at, tick_count, pid, last_error)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(process_name) DO UPDATE SET
                    last_tick_at = excluded.last_tick_at,
                    tick_count   = excluded.tick_count,
                    pid          = excluded.pid,
                    last_error   = excluded.last_error
                """,
                (process_name, ts, tick_count, pid if pid is not None else os.getpid(), last_error),
            )

    def read(self, process_name: str) -> Heartbeat | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM watchdog_heartbeat WHERE process_name = ?",
                (process_name,),
            ).fetchone()
        if row is None:
            return None
        return Heartbeat(
            process_name=row["process_name"],
            last_tick_at=datetime.fromisoformat(row["last_tick_at"]),
            tick_count=row["tick_count"],
            pid=row["pid"],
            last_error=row["last_error"],
        )

    def all(self) -> list[Heartbeat]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM watchdog_heartbeat ORDER BY process_name"
            ).fetchall()
        return [
            Heartbeat(
                process_name=r["process_name"],
                last_tick_at=datetime.fromisoformat(r["last_tick_at"]),
                tick_count=r["tick_count"],
                pid=r["pid"],
                last_error=r["last_error"],
            )
            for r in rows
        ]
