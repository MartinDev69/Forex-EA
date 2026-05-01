"""SQLite trade journal.

Every open/close is persisted so the FastAPI /trades endpoint can read them
and AntiGreed can show history. One row per trade lifecycle.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.strategies.base import SignalType

from .base import Order, OrderStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    lot_size        REAL NOT NULL,
    entry_price     REAL NOT NULL,
    exit_price      REAL,
    stop_loss       REAL NOT NULL,
    take_profit     REAL NOT NULL,
    strategy        TEXT NOT NULL,
    status          TEXT NOT NULL,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT,
    pnl             REAL NOT NULL DEFAULT 0,
    close_reason    TEXT,
    broker_ticket   INTEGER
);
"""


class TradeJournal:
    def __init__(self, db_path: Path | str = "data/trades.db") -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def record_open(self, order: Order) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO trades (id, symbol, side, lot_size, entry_price,
                    stop_loss, take_profit, strategy, status, opened_at,
                    broker_ticket)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.id,
                    order.symbol,
                    order.side.value,
                    order.lot_size,
                    order.entry_price,
                    order.stop_loss,
                    order.take_profit,
                    order.strategy,
                    order.status.value,
                    order.opened_at.isoformat(),
                    order.broker_ticket,
                ),
            )

    def record_close(self, order: Order) -> None:
        with self._conn() as c:
            c.execute(
                """
                UPDATE trades SET status = ?, exit_price = ?, closed_at = ?,
                    pnl = ?, close_reason = ?
                WHERE id = ?
                """,
                (
                    order.status.value,
                    order.exit_price,
                    order.closed_at.isoformat() if order.closed_at else None,
                    order.pnl,
                    order.close_reason,
                    order.id,
                ),
            )

    def recent(self, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def summary_today(self) -> dict:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                       COALESCE(SUM(pnl), 0) AS total_pnl
                FROM trades
                WHERE status = ? AND DATE(closed_at) = ?
                """,
                (OrderStatus.CLOSED.value, today),
            ).fetchone()
        return {"total": row["total"], "wins": row["wins"] or 0, "pnl": row["total_pnl"]}

    def summary_window(self, days: int) -> dict:
        """Aggregate closed trades over the last `days` UTC days, plus best/
        worst pair and best strategy by P&L. Used by the weekly digest."""
        with self._conn() as c:
            agg = c.execute(
                f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                       COALESCE(SUM(pnl), 0) AS total_pnl
                FROM trades
                WHERE status = ?
                  AND closed_at >= datetime('now', '-{int(days)} days')
                """,
                (OrderStatus.CLOSED.value,),
            ).fetchone()
            by_symbol = c.execute(
                f"""
                SELECT symbol, COALESCE(SUM(pnl), 0) AS pnl
                FROM trades
                WHERE status = ?
                  AND closed_at >= datetime('now', '-{int(days)} days')
                GROUP BY symbol
                ORDER BY pnl DESC
                """,
                (OrderStatus.CLOSED.value,),
            ).fetchall()
            by_strategy = c.execute(
                f"""
                SELECT strategy, COALESCE(SUM(pnl), 0) AS pnl,
                       COUNT(*) AS trades
                FROM trades
                WHERE status = ?
                  AND closed_at >= datetime('now', '-{int(days)} days')
                GROUP BY strategy
                ORDER BY pnl DESC
                """,
                (OrderStatus.CLOSED.value,),
            ).fetchall()
        best_symbol = by_symbol[0]["symbol"] if by_symbol else None
        worst_symbol = by_symbol[-1]["symbol"] if len(by_symbol) > 1 else None
        best_strategy = by_strategy[0]["strategy"] if by_strategy else None
        return {
            "total": agg["total"],
            "wins": agg["wins"] or 0,
            "pnl": agg["total_pnl"],
            "best_symbol": best_symbol,
            "worst_symbol": worst_symbol,
            "best_strategy": best_strategy,
        }
