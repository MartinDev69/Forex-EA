"""Persistent dedup of which bar each (strategy, symbol) last opened on.

Without this, restarting the bot re-evaluates the most recent in-progress
M15 bar and any strategy whose entry condition still holds on that bar
fires again — so every `nssm restart` opens a fresh wave of trades. We
persist the bar timestamp the bot acted on per strategy/symbol; on the
next signal we refuse to open if the bar matches.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_signal_dedup (
    strategy     TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    last_bar_ts  TEXT NOT NULL,
    last_side    TEXT NOT NULL,
    PRIMARY KEY (strategy, symbol)
);
"""


class SignalDedupStore:
    def __init__(self, db_path: Path | str = "data/trades.db") -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def already_acted(self, strategy: str, symbol: str, bar_ts: datetime) -> bool:
        """True if the bot has already opened on this exact bar for this
        (strategy, symbol). The bar timestamp comparison is on the ISO
        string so timezone awareness round-trips cleanly.
        """
        target = bar_ts.isoformat()
        with self._conn() as c:
            row = c.execute(
                "SELECT last_bar_ts FROM bot_signal_dedup "
                "WHERE strategy = ? AND symbol = ?",
                (strategy, symbol),
            ).fetchone()
        if row is None:
            return False
        return row["last_bar_ts"] == target

    def remember(self, strategy: str, symbol: str, bar_ts: datetime, side: str) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO bot_signal_dedup (strategy, symbol, last_bar_ts, last_side)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(strategy, symbol) DO UPDATE SET
                    last_bar_ts = excluded.last_bar_ts,
                    last_side = excluded.last_side
                """,
                (strategy, symbol, bar_ts.isoformat(), side),
            )
