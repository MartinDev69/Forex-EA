"""Persistent state for the prop-firm guard.

Single-row table — initial_balance, peak_equity, daily_start_equity, kill flags,
trading_days_count. The bot updates state through the guard, never directly.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS propfirm_state (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    initial_balance       REAL NOT NULL,
    peak_equity           REAL NOT NULL,
    daily_start_equity    REAL NOT NULL,
    daily_start_date      TEXT NOT NULL,        -- ISO date (UTC) for the current trading day
    trading_days_count    INTEGER NOT NULL DEFAULT 0,
    last_trading_date     TEXT,                  -- ISO date when last trade was opened
    killed_today          INTEGER NOT NULL DEFAULT 0,
    killed_permanently    INTEGER NOT NULL DEFAULT 0,
    killed_reason         TEXT,
    updated_at            TEXT NOT NULL
);
"""


@dataclass
class PropFirmState:
    initial_balance: float
    peak_equity: float
    daily_start_equity: float
    daily_start_date: date
    trading_days_count: int
    last_trading_date: date | None
    killed_today: bool
    killed_permanently: bool
    killed_reason: str | None
    updated_at: datetime


class PropFirmStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def read(self) -> PropFirmState | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM propfirm_state WHERE id = 1").fetchone()
        if row is None:
            return None
        return _row_to_state(row)

    def initialize(self, initial_balance: float, today: date, now: datetime) -> PropFirmState:
        """Seed the single state row. Called on first run with the operator's
        starting balance — usually pulled from the broker on bot startup.
        Idempotent: if state already exists, returns it unchanged.
        """
        existing = self.read()
        if existing is not None:
            return existing
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO propfirm_state
                    (id, initial_balance, peak_equity, daily_start_equity,
                     daily_start_date, trading_days_count, killed_today,
                     killed_permanently, updated_at)
                VALUES (1, ?, ?, ?, ?, 0, 0, 0, ?)
                """,
                (initial_balance, initial_balance, initial_balance,
                 today.isoformat(), now.isoformat()),
            )
        return self.read()

    def write(self, state: PropFirmState) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO propfirm_state
                    (id, initial_balance, peak_equity, daily_start_equity,
                     daily_start_date, trading_days_count, last_trading_date,
                     killed_today, killed_permanently, killed_reason, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    initial_balance     = excluded.initial_balance,
                    peak_equity         = excluded.peak_equity,
                    daily_start_equity  = excluded.daily_start_equity,
                    daily_start_date    = excluded.daily_start_date,
                    trading_days_count  = excluded.trading_days_count,
                    last_trading_date   = excluded.last_trading_date,
                    killed_today        = excluded.killed_today,
                    killed_permanently  = excluded.killed_permanently,
                    killed_reason       = excluded.killed_reason,
                    updated_at          = excluded.updated_at
                """,
                (
                    state.initial_balance,
                    state.peak_equity,
                    state.daily_start_equity,
                    state.daily_start_date.isoformat(),
                    state.trading_days_count,
                    state.last_trading_date.isoformat() if state.last_trading_date else None,
                    1 if state.killed_today else 0,
                    1 if state.killed_permanently else 0,
                    state.killed_reason,
                    state.updated_at.isoformat(),
                ),
            )

    def reset(self) -> None:
        """Operator-only: wipe all state. Use after a new challenge starts."""
        with self._conn() as c:
            c.execute("DELETE FROM propfirm_state WHERE id = 1")


def _row_to_state(row: sqlite3.Row) -> PropFirmState:
    return PropFirmState(
        initial_balance=float(row["initial_balance"]),
        peak_equity=float(row["peak_equity"]),
        daily_start_equity=float(row["daily_start_equity"]),
        daily_start_date=date.fromisoformat(row["daily_start_date"]),
        trading_days_count=int(row["trading_days_count"]),
        last_trading_date=date.fromisoformat(row["last_trading_date"]) if row["last_trading_date"] else None,
        killed_today=bool(row["killed_today"]),
        killed_permanently=bool(row["killed_permanently"]),
        killed_reason=row["killed_reason"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
