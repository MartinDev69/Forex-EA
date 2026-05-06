"""Shared strategy on/off + mode flags, persisted in SQLite.

Two flags per strategy:
- ``enabled``: master switch. When False the strategy is silenced
  completely (no signals generated, no alerts).
- ``mode``: one of ``'execute'`` (bot places orders, default) or
  ``'signal'`` (bot only fires Telegram alerts, doesn't trade).

The bot process and the FastAPI process both need to know these, so the
flags live in the same SQLite file the trade journal uses — each side
opens its own connection.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# Modes a strategy can run in. Anything else gets coerced to 'execute'
# on read so a typo in the DB doesn't silently disable trading.
MODE_EXECUTE = "execute"
MODE_SIGNAL = "signal"
VALID_MODES = (MODE_EXECUTE, MODE_SIGNAL)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_toggles (
    name     TEXT PRIMARY KEY,
    enabled  INTEGER NOT NULL,
    mode     TEXT NOT NULL DEFAULT 'execute'
);
"""

DEFAULT_STRATEGY_FLAGS: dict[str, bool] = {
    # The original three default to enabled+execute so a fresh deploy
    # behaves like before. The nine new strategies start disarmed so the
    # user opts each one in deliberately rather than waking up to twelve
    # strategies all firing at once. They're still in the toggle store so
    # they show up in the UI ready to be turned on.
    "ma_crossover": True,
    "rsi_mean_reversion": True,
    "donchian_breakout": True,
    "macd_cross": False,
    "bollinger_bounce": False,
    "bollinger_squeeze": False,
    "stochastic_reversal": False,
    "triple_ma_alignment": False,
    "inside_bar_breakout": False,
    "engulfing_pattern": False,
    "ema_pullback": False,
    "adx_breakout": False,
}


class StrategyToggleStore:
    def __init__(self, db_path: Path | str = "data/trades.db") -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._ensure_mode_column(c)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _ensure_mode_column(c: sqlite3.Connection) -> None:
        """Add the ``mode`` column to pre-existing tables that predate it.

        SQLite's ``CREATE TABLE IF NOT EXISTS`` doesn't add columns to an
        already-created table, so we have to ALTER explicitly.
        """
        cols = {row["name"] for row in c.execute("PRAGMA table_info(strategy_toggles)")}
        if "mode" not in cols:
            c.execute(
                "ALTER TABLE strategy_toggles "
                "ADD COLUMN mode TEXT NOT NULL DEFAULT 'execute'"
            )

    def initialize_defaults(self, defaults: dict[str, bool]) -> None:
        """Insert rows for any strategy not already present. Won't clobber existing flags."""
        with self._conn() as c:
            for name, enabled in defaults.items():
                c.execute(
                    "INSERT OR IGNORE INTO strategy_toggles (name, enabled, mode) "
                    "VALUES (?, ?, 'execute')",
                    (name, 1 if enabled else 0),
                )

    def list(self) -> dict[str, bool]:
        """Legacy: name → enabled. Use ``list_full()`` for mode info."""
        with self._conn() as c:
            rows = c.execute("SELECT name, enabled FROM strategy_toggles ORDER BY name").fetchall()
        return {r["name"]: bool(r["enabled"]) for r in rows}

    def list_full(self) -> list[dict]:
        """Return [{name, enabled, mode}] sorted by name."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT name, enabled, mode FROM strategy_toggles ORDER BY name"
            ).fetchall()
        return [
            {
                "name": r["name"],
                "enabled": bool(r["enabled"]),
                "mode": self._sanitize_mode(r["mode"]),
            }
            for r in rows
        ]

    def is_enabled(self, name: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT enabled FROM strategy_toggles WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return False
        return bool(row["enabled"])

    def get_mode(self, name: str) -> str:
        with self._conn() as c:
            row = c.execute(
                "SELECT mode FROM strategy_toggles WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return MODE_EXECUTE
        return self._sanitize_mode(row["mode"])

    def get_full(self, name: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT name, enabled, mode FROM strategy_toggles WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return None
        return {
            "name": row["name"],
            "enabled": bool(row["enabled"]),
            "mode": self._sanitize_mode(row["mode"]),
        }

    def set(self, name: str, enabled: bool) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO strategy_toggles (name, enabled, mode) VALUES (?, ?, 'execute')
                ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled
                """,
                (name, 1 if enabled else 0),
            )

    def set_mode(self, name: str, mode: str) -> str:
        """Change a strategy's mode. Returns the mode that was actually
        written (sanitized). Raises KeyError if the strategy is unknown.
        """
        clean = self._sanitize_mode(mode)
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM strategy_toggles WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                raise KeyError(name)
            c.execute(
                "UPDATE strategy_toggles SET mode = ? WHERE name = ?",
                (clean, name),
            )
        return clean

    def toggle(self, name: str) -> bool:
        """Flip a strategy's enabled flag. Raises KeyError if unknown."""
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

    @staticmethod
    def _sanitize_mode(mode: str | None) -> str:
        if mode in VALID_MODES:
            return mode
        return MODE_EXECUTE
