"""Shared strategy on/off flags, persisted in SQLite.

The bot process and the FastAPI process both need to know which strategies
are enabled. They don't share memory, so we write the flags to the same
SQLite file the trade journal uses — each side opens its own connection.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_toggles (
    name     TEXT PRIMARY KEY,
    enabled  INTEGER NOT NULL
);
"""

DEFAULT_STRATEGY_FLAGS: dict[str, bool] = {
    "ma_crossover": True,
    "rsi_mean_reversion": False,
    "donchian_breakout": False,
}


class StrategyToggleStore:
    def __init__(self, db_path: Path | str = "data/trades.db") -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize_defaults(self, defaults: dict[str, bool]) -> None:
        """Insert rows for any strategy not already present. Won't clobber existing flags."""
        with self._conn() as c:
            for name, enabled in defaults.items():
                c.execute(
                    "INSERT OR IGNORE INTO strategy_toggles (name, enabled) VALUES (?, ?)",
                    (name, 1 if enabled else 0),
                )

    def list(self) -> dict[str, bool]:
        with self._conn() as c:
            rows = c.execute("SELECT name, enabled FROM strategy_toggles ORDER BY name").fetchall()
        return {r["name"]: bool(r["enabled"]) for r in rows}

    def is_enabled(self, name: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT enabled FROM strategy_toggles WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return False
        return bool(row["enabled"])

    def set(self, name: str, enabled: bool) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO strategy_toggles (name, enabled) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled
                """,
                (name, 1 if enabled else 0),
            )

    def toggle(self, name: str) -> bool:
        """Flip a strategy's flag. Raises KeyError if the strategy is unknown."""
        with self._conn() as c:
            row = c.execute(
                "SELECT enabled FROM strategy_toggles WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                raise KeyError(name)
            new_value = 0 if row["enabled"] else 1
            c.execute(
                "UPDATE strategy_toggles SET enabled = ? WHERE name = ?",
                (new_value, name),
            )
        return bool(new_value)
