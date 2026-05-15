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

-- One row per SL/TP modification on an existing position. The signal
-- feed unions these into the event stream so operator EAs can mirror
-- admin's trailing-stop adjustments — without this, operator positions
-- keep the original stop and slowly diverge from admin's risk profile.
CREATE TABLE IF NOT EXISTS trade_modifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL,
    ts              TEXT NOT NULL,
    stop_loss       REAL,
    take_profit     REAL
);
CREATE INDEX IF NOT EXISTS ix_trade_modifications_ts
    ON trade_modifications(ts);
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

    def record_modify(
        self,
        trade_id: int,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        now: datetime | None = None,
    ) -> None:
        """Log a SL/TP modification so the signal feed can emit a MODIFY
        event to operator EAs. Both fields may be present or only one —
        the EA applies whichever non-null values it receives.
        """
        now = now or datetime.now(timezone.utc)
        with self._conn() as c:
            c.execute(
                "INSERT INTO trade_modifications "
                "(trade_id, ts, stop_loss, take_profit) VALUES (?,?,?,?)",
                (trade_id, now.isoformat(), stop_loss, take_profit),
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

    def list_open(self) -> list[dict]:
        """Every journal row currently marked OPEN with a broker ticket.
        Used by the bot's auto-reconciler to detect entries that have
        already closed broker-side but never got their journal update.
        """
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT id, symbol, side, broker_ticket, entry_price, opened_at
                FROM trades
                WHERE status = 'OPEN' AND closed_at IS NULL
                  AND broker_ticket IS NOT NULL
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_closed_by_ticket(
        self,
        broker_ticket: int,
        *,
        exit_price: float,
        closed_at: datetime,
        pnl: float,
        close_reason: str = "reconciled_from_mt5",
    ) -> None:
        """Patch an open journal row to CLOSED — used by the auto-
        reconciler when the broker has already closed the position.
        Only touches rows that are still marked OPEN so re-running is a
        no-op once the row is settled.
        """
        with self._conn() as c:
            c.execute(
                """
                UPDATE trades
                SET status = 'CLOSED',
                    exit_price = ?,
                    closed_at = ?,
                    pnl = ?,
                    close_reason = COALESCE(close_reason, ?)
                WHERE broker_ticket = ? AND status = 'OPEN'
                """,
                (
                    float(exit_price),
                    closed_at.isoformat(),
                    float(pnl),
                    close_reason,
                    int(broker_ticket),
                ),
            )

    def recent(self, limit: int = 20, since_iso: str | None = None) -> list[dict]:
        """Most recent trades, newest first. Pass since_iso to clip the
        window to trades opened on/after that timestamp — used for
        copy-trading operators so they only see trades from when their
        EA was online.
        """
        with self._conn() as c:
            if since_iso:
                rows = c.execute(
                    "SELECT * FROM trades WHERE opened_at >= ? "
                    "ORDER BY opened_at DESC LIMIT ?",
                    (since_iso, limit),
                ).fetchall()
            else:
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
