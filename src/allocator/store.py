"""Persist the allocator's most recent decisions.

One row per (strategy, symbol). Upserted on each refresh so the API can
return the current allocation without re-running the scorer. Cheap on
disk: a single small table, no time-series bloat.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .allocator import Allocation


_SCHEMA = """
CREATE TABLE IF NOT EXISTS allocations (
    strategy        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    role            TEXT NOT NULL,
    weight          REAL NOT NULL,
    sample_size     INTEGER NOT NULL,
    avg_r           REAL NOT NULL,
    win_rate        REAL NOT NULL,
    note            TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (strategy, symbol)
);
"""


class AllocationStore:
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

    def upsert_many(self, allocations: list[Allocation]) -> None:
        if not allocations:
            return
        with self._conn() as c:
            c.executemany(
                """
                INSERT INTO allocations (strategy, symbol, role, weight,
                    sample_size, avg_r, win_rate, note, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy, symbol) DO UPDATE SET
                    role = excluded.role,
                    weight = excluded.weight,
                    sample_size = excluded.sample_size,
                    avg_r = excluded.avg_r,
                    win_rate = excluded.win_rate,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        a.strategy,
                        a.symbol,
                        a.role,
                        a.weight,
                        a.sample_size,
                        a.avg_r,
                        a.win_rate,
                        a.note,
                        a.updated_at,
                    )
                    for a in allocations
                ],
            )

    def all(self) -> list[Allocation]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT strategy, symbol, role, weight, sample_size, avg_r,
                       win_rate, note, updated_at
                FROM allocations
                ORDER BY strategy, symbol
                """
            ).fetchall()
        return [
            Allocation(
                strategy=r["strategy"],
                symbol=r["symbol"],
                role=r["role"],
                weight=r["weight"],
                sample_size=r["sample_size"],
                avg_r=r["avg_r"],
                win_rate=r["win_rate"],
                note=r["note"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def get(self, strategy: str, symbol: str) -> Allocation | None:
        with self._conn() as c:
            row = c.execute(
                """
                SELECT strategy, symbol, role, weight, sample_size, avg_r,
                       win_rate, note, updated_at
                FROM allocations
                WHERE strategy = ? AND symbol = ?
                """,
                (strategy, symbol),
            ).fetchone()
        if row is None:
            return None
        return Allocation(
            strategy=row["strategy"],
            symbol=row["symbol"],
            role=row["role"],
            weight=row["weight"],
            sample_size=row["sample_size"],
            avg_r=row["avg_r"],
            win_rate=row["win_rate"],
            note=row["note"],
            updated_at=row["updated_at"],
        )
