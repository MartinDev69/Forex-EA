"""Persistent OHLC paths for replay.

One row per bar within a trade's open→close window. Composite primary key
keeps lookups by trade_id ordered without an explicit index. Bars stored
in chronological order; the engine walks them in the order returned.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_paths (
    trade_id   INTEGER NOT NULL,
    bar_index  INTEGER NOT NULL,
    ts         TEXT NOT NULL,
    open       REAL NOT NULL,
    high       REAL NOT NULL,
    low        REAL NOT NULL,
    close      REAL NOT NULL,
    PRIMARY KEY (trade_id, bar_index)
);
"""


@dataclass(frozen=True)
class PathBar:
    ts: str
    open: float
    high: float
    low: float
    close: float


class PathStore:
    def __init__(self, db_path: Path | str = "data/trades.db") -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def write(self, trade_id: int, bars: list[PathBar]) -> None:
        if not bars:
            return
        with self._conn() as c:
            # Wipe any prior rows for this trade — recording is idempotent so
            # a re-call replaces the path rather than duplicating it.
            c.execute("DELETE FROM trade_paths WHERE trade_id = ?", (trade_id,))
            c.executemany(
                """
                INSERT INTO trade_paths
                    (trade_id, bar_index, ts, open, high, low, close)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (trade_id, i, b.ts, b.open, b.high, b.low, b.close)
                    for i, b in enumerate(bars)
                ],
            )

    def read(self, trade_id: int) -> list[PathBar]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT ts, open, high, low, close
                FROM trade_paths
                WHERE trade_id = ?
                ORDER BY bar_index ASC
                """,
                (trade_id,),
            ).fetchall()
        return [
            PathBar(ts=r["ts"], open=r["open"], high=r["high"], low=r["low"], close=r["close"])
            for r in rows
        ]
