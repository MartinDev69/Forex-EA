"""Broker/MT5 heartbeat — written by the bot process, read by the API process.

Both processes share the same SQLite file, so the API can show whether the bot
is currently connected to MT5 without needing IPC. The bot writes a row on
every successful tick; `stale_s` lets callers decide if it's still fresh.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_status (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    connected       INTEGER NOT NULL,
    broker          TEXT,
    server          TEXT,
    login           INTEGER,
    account_info    TEXT,
    last_error      TEXT,
    updated_at      TEXT NOT NULL
);
"""


@dataclass
class BrokerStatus:
    connected: bool
    broker: str | None
    server: str | None
    login: int | None
    account_info: dict | None
    last_error: str | None
    updated_at: datetime


class BrokerStatusStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def write(self, *, connected: bool, broker: str | None = None, server: str | None = None,
              login: int | None = None, account_info: dict | None = None,
              last_error: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO broker_status (id, connected, broker, server, login,
                                           account_info, last_error, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    connected = excluded.connected,
                    broker = excluded.broker,
                    server = excluded.server,
                    login = excluded.login,
                    account_info = excluded.account_info,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (
                    1 if connected else 0, broker, server, login,
                    json.dumps(account_info) if account_info else None,
                    last_error, now,
                ),
            )

    def read(self) -> BrokerStatus | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM broker_status WHERE id = 1").fetchone()
        if row is None:
            return None
        return BrokerStatus(
            connected=bool(row["connected"]),
            broker=row["broker"],
            server=row["server"],
            login=row["login"],
            account_info=json.loads(row["account_info"]) if row["account_info"] else None,
            last_error=row["last_error"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
