"""SQLite-backed store for the most recent regime snapshot per symbol.

The bot writes on every tick (in-process with the data feed); the API reads
for the dashboard. Only the latest snapshot per symbol is retained — regime
history isn't interesting for the UI and we don't want this table to grow
unbounded.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .classifier import RegimeSnapshot, TrendRegime, VolatilityRegime


_SCHEMA = """
CREATE TABLE IF NOT EXISTS regime_snapshots (
    symbol TEXT PRIMARY KEY,
    trend TEXT NOT NULL,
    volatility TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class RegimeStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def upsert(self, symbol: str, snapshot: RegimeSnapshot) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(snapshot.to_dict())
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO regime_snapshots (symbol, trend, volatility, payload, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    trend = excluded.trend,
                    volatility = excluded.volatility,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (symbol, snapshot.trend.value, snapshot.volatility.value, payload, now),
            )

    def get(self, symbol: str) -> dict | None:
        """Return the stored snapshot dict for `symbol`, or None if missing."""
        with self._conn() as c:
            row = c.execute(
                "SELECT payload, updated_at FROM regime_snapshots WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        data["stored_at"] = row[1]
        return data

    def all(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT payload, updated_at FROM regime_snapshots ORDER BY symbol"
            ).fetchall()
        out = []
        for payload, updated_at in rows:
            d = json.loads(payload)
            d["stored_at"] = updated_at
            out.append(d)
        return out


def empty_snapshot_dict(symbol: str) -> dict:
    """Shape the API returns when we've never classified this symbol yet."""
    return {
        "trend": TrendRegime.UNKNOWN.value,
        "volatility": VolatilityRegime.UNKNOWN.value,
        "label": "unknown",
        "adx": None,
        "plus_di": None,
        "minus_di": None,
        "atr": None,
        "atr_pct": None,
        "timestamp": None,
        "stored_at": None,
        "symbol": symbol,
    }
