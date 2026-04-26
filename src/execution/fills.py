"""Per-fill execution-quality log.

For every order open and close we record a Fill row capturing requested
vs filled price (slippage), latency, and the broker ticket. The dashboard
reads aggregates from this table to surface broker quality drift —
widening spreads, slow fills, or a sudden run of rejects almost always
predict a worse trading week.

Cost profile:
- One INSERT per fill on top of the existing journal write. At retail
  volumes (a few fills per hour), this is well under a millisecond per
  trade and SQLite WAL absorbs it without blocking.
- Stats are computed via SQL GROUP BY on a small table — no in-process
  rolling buffers, so memory stays flat regardless of trade history.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    event           TEXT NOT NULL,    -- 'OPEN' | 'CLOSE'
    requested_price REAL NOT NULL,
    filled_price    REAL,             -- NULL when rejected
    slippage_pips   REAL,             -- adverse = positive
    latency_ms      REAL NOT NULL,
    broker_ticket   INTEGER,
    status          TEXT NOT NULL,    -- 'FILLED' | 'REJECTED'
    reason          TEXT,
    filled_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fills_filled_at ON fills(filled_at);
CREATE INDEX IF NOT EXISTS ix_fills_symbol ON fills(symbol);
"""


def pip_size(symbol: str) -> float:
    """Pip definition matching the rest of the codebase: 0.0001 for FX,
    0.01 for JPY pairs and most metals/indices.
    """
    s = symbol.upper()
    if "JPY" in s or "XAU" in s or s.endswith("USD") and s.startswith("XA"):
        return 0.01
    return 0.0001


def signed_slippage_pips(symbol: str, side: str, requested: float, filled: float) -> float:
    """Slippage in pips, signed so positive = adverse to the trader.

    For a BUY, paying more than requested is bad → positive.
    For a SELL, getting less than requested is bad → positive.
    """
    raw = filled - requested
    if side.upper() == "SELL":
        raw = -raw
    return raw / pip_size(symbol)


@dataclass(frozen=True)
class Fill:
    trade_id: int | None
    symbol: str
    side: str
    event: Literal["OPEN", "CLOSE"]
    requested_price: float
    filled_price: float | None
    slippage_pips: float | None
    latency_ms: float
    broker_ticket: int | None
    status: Literal["FILLED", "REJECTED"]
    reason: str | None
    filled_at: datetime


@dataclass(frozen=True)
class SymbolStats:
    symbol: str
    fill_count: int
    rejected_count: int
    avg_slippage_pips: float
    max_slippage_pips: float
    avg_latency_ms: float
    p95_latency_ms: float


class FillStore:
    def __init__(self, db_path: Path | str = "data/trades.db") -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def record(self, fill: Fill) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO fills (trade_id, symbol, side, event, requested_price,
                    filled_price, slippage_pips, latency_ms, broker_ticket,
                    status, reason, filled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.trade_id, fill.symbol, fill.side, fill.event,
                    fill.requested_price, fill.filled_price, fill.slippage_pips,
                    fill.latency_ms, fill.broker_ticket, fill.status,
                    fill.reason, fill.filled_at.isoformat(),
                ),
            )

    def recent(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM fills ORDER BY filled_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self, since_hours: int | None = None) -> list[SymbolStats]:
        """Per-symbol aggregates over filled (status='FILLED') rows.

        `since_hours` filters to the last N hours so the dashboard can show a
        rolling window without dragging in months of warmup history.
        """
        where = "status = 'FILLED'"
        params: list = []
        if since_hours is not None:
            cutoff = datetime.now(timezone.utc).timestamp() - since_hours * 3600
            cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
            where += " AND filled_at >= ?"
            params.append(cutoff_iso)

        with self._conn() as c:
            rows = c.execute(
                f"""
                SELECT symbol,
                       COUNT(*) AS fill_count,
                       AVG(slippage_pips) AS avg_slip,
                       MAX(slippage_pips) AS max_slip,
                       AVG(latency_ms) AS avg_latency
                FROM fills
                WHERE {where}
                GROUP BY symbol
                ORDER BY symbol
                """,
                params,
            ).fetchall()

            # p95 latency requires a second pass per symbol — still cheap given
            # fills tables stay small (thousands of rows even after years).
            rejected_counts = {
                r["symbol"]: r["c"] for r in c.execute(
                    "SELECT symbol, COUNT(*) AS c FROM fills WHERE status = 'REJECTED' GROUP BY symbol"
                ).fetchall()
            }
            p95 = {}
            for r in rows:
                lat = c.execute(
                    """
                    SELECT latency_ms FROM fills
                    WHERE symbol = ? AND status = 'FILLED'
                    ORDER BY latency_ms ASC
                    """,
                    (r["symbol"],),
                ).fetchall()
                if lat:
                    idx = max(0, int(0.95 * len(lat)) - 1)
                    p95[r["symbol"]] = lat[idx]["latency_ms"]

        return [
            SymbolStats(
                symbol=r["symbol"],
                fill_count=r["fill_count"],
                rejected_count=rejected_counts.get(r["symbol"], 0),
                avg_slippage_pips=r["avg_slip"] or 0.0,
                max_slippage_pips=r["max_slip"] or 0.0,
                avg_latency_ms=r["avg_latency"] or 0.0,
                p95_latency_ms=p95.get(r["symbol"], 0.0),
            )
            for r in rows
        ]
