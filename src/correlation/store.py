"""SQLite store for pairwise correlations.

Single-row-per-pair table keyed on the unordered (a, b) pair (alphabetic
order is enforced on write). The bot writes the latest matrix on a refresh
cycle; the API/throttle read individual pairs or the whole table.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


_SCHEMA = """
CREATE TABLE IF NOT EXISTS correlation_pairs (
    symbol_a TEXT NOT NULL,
    symbol_b TEXT NOT NULL,
    value REAL NOT NULL,
    window_bars INTEGER NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (symbol_a, symbol_b)
);
"""


def _ordered(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


class CorrelationStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def upsert_matrix(self, matrix: pd.DataFrame, window_bars: int) -> int:
        """Persist the upper triangle of `matrix` (excluding diagonal).

        Returns the number of pair rows written.
        """
        if matrix.empty:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        rows: list[tuple[str, str, float, int, str]] = []
        symbols = list(matrix.columns)
        for i, a in enumerate(symbols):
            for b in symbols[i + 1:]:
                v = matrix.loc[a, b]
                if pd.isna(v):
                    continue
                sa, sb = _ordered(a, b)
                rows.append((sa, sb, float(v), int(window_bars), now))
        if not rows:
            return 0
        with self._conn() as c:
            c.executemany(
                """
                INSERT INTO correlation_pairs (symbol_a, symbol_b, value, window_bars, computed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol_a, symbol_b) DO UPDATE SET
                    value = excluded.value,
                    window_bars = excluded.window_bars,
                    computed_at = excluded.computed_at
                """,
                rows,
            )
        return len(rows)

    def pair(self, a: str, b: str) -> float | None:
        if a == b:
            return 1.0
        sa, sb = _ordered(a, b)
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM correlation_pairs WHERE symbol_a = ? AND symbol_b = ?",
                (sa, sb),
            ).fetchone()
        return float(row[0]) if row else None

    def all_pairs(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT symbol_a, symbol_b, value, window_bars, computed_at
                FROM correlation_pairs
                ORDER BY ABS(value) DESC, symbol_a, symbol_b
                """
            ).fetchall()
        return [
            {
                "symbol_a": a, "symbol_b": b, "value": v,
                "window_bars": w, "computed_at": ts,
            }
            for a, b, v, w, ts in rows
        ]

    def matrix(self, symbols: list[str]) -> pd.DataFrame:
        """Reconstruct a square matrix for `symbols`. Diagonal = 1, missing = NaN."""
        out = pd.DataFrame(index=symbols, columns=symbols, dtype=float)
        for s in symbols:
            out.loc[s, s] = 1.0
        with self._conn() as c:
            rows = c.execute(
                "SELECT symbol_a, symbol_b, value FROM correlation_pairs"
            ).fetchall()
        wanted = set(symbols)
        for a, b, v in rows:
            if a in wanted and b in wanted:
                out.loc[a, b] = v
                out.loc[b, a] = v
        return out
