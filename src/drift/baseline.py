"""Per-(strategy, symbol) baseline metrics from backtest, persisted in SQLite.

The DriftMonitor compares live trade outcomes against these baselines to
catch alpha decay. Baselines are seeded from a backtest run via
`scripts/build_baseline.py` and refreshed manually — they don't change
during normal bot operation.

Schema is single-table with PRIMARY KEY (strategy, symbol) so re-seeding
is a clean upsert.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drift_baselines (
    strategy            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    trade_count         INTEGER NOT NULL,
    win_rate            REAL NOT NULL,
    avg_r               REAL NOT NULL,
    avg_trades_per_day  REAL NOT NULL,
    source              TEXT NOT NULL,
    computed_at         TEXT NOT NULL,
    PRIMARY KEY (strategy, symbol)
);
"""


@dataclass(frozen=True)
class Baseline:
    strategy: str
    symbol: str
    trade_count: int
    win_rate: float
    avg_r: float
    avg_trades_per_day: float
    source: str  # 'backtest' | 'manual'
    computed_at: datetime

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "trade_count": self.trade_count,
            "win_rate": self.win_rate,
            "avg_r": self.avg_r,
            "avg_trades_per_day": self.avg_trades_per_day,
            "source": self.source,
            "computed_at": self.computed_at.isoformat(),
        }


class BaselineStore:
    def __init__(self, db_path: Path | str = "data/trades.db") -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        # WAL keeps reads cheap while the bot writes elsewhere in the file.
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def upsert(self, b: Baseline) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO drift_baselines
                    (strategy, symbol, trade_count, win_rate, avg_r,
                     avg_trades_per_day, source, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy, symbol) DO UPDATE SET
                    trade_count = excluded.trade_count,
                    win_rate = excluded.win_rate,
                    avg_r = excluded.avg_r,
                    avg_trades_per_day = excluded.avg_trades_per_day,
                    source = excluded.source,
                    computed_at = excluded.computed_at
                """,
                (
                    b.strategy,
                    b.symbol,
                    b.trade_count,
                    b.win_rate,
                    b.avg_r,
                    b.avg_trades_per_day,
                    b.source,
                    b.computed_at.isoformat(),
                ),
            )

    def get(self, strategy: str, symbol: str) -> Baseline | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM drift_baselines WHERE strategy = ? AND symbol = ?",
                (strategy, symbol),
            ).fetchone()
        return self._row_to_baseline(row) if row else None

    def all(self) -> list[Baseline]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM drift_baselines").fetchall()
        return [self._row_to_baseline(r) for r in rows]

    def delete(self, strategy: str, symbol: str) -> None:
        with self._conn() as c:
            c.execute(
                "DELETE FROM drift_baselines WHERE strategy = ? AND symbol = ?",
                (strategy, symbol),
            )

    @staticmethod
    def _row_to_baseline(row: sqlite3.Row) -> Baseline:
        return Baseline(
            strategy=row["strategy"],
            symbol=row["symbol"],
            trade_count=row["trade_count"],
            win_rate=row["win_rate"],
            avg_r=row["avg_r"],
            avg_trades_per_day=row["avg_trades_per_day"],
            source=row["source"],
            computed_at=_parse_dt(row["computed_at"]),
        )


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
