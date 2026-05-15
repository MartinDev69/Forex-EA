"""Per-operator trade fills reported by the AntiGreedCopier EA.

Copy-trading operators run MT5 on their own machine, so admin's bot
journal isn't a faithful record of what hit *their* account — different
lot sizing, broker fees, and especially a different account currency.
Reading PnL from admin's journal and showing it to a ZAR operator gave
USD numbers that didn't match their MT5 statement.

This table is the operator-side counterpart of ``trades`` in
``journal.py``. The EA POSTs once when it opens a copy (status=OPEN, no
exit/pnl yet) and again when it closes (status=CLOSED, exit/pnl
filled). ``broker_ticket`` is the MT5 position ticket on the operator's
own account and is the dedup key — if the EA retries an Open report we
just update the row rather than inserting twice.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ea_fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL,
    broker_ticket   INTEGER NOT NULL,
    master_trade_id INTEGER,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    lot_size        REAL NOT NULL,
    entry_price     REAL NOT NULL,
    exit_price      REAL,
    stop_loss       REAL,
    take_profit     REAL,
    pnl             REAL,
    strategy        TEXT,
    status          TEXT NOT NULL,
    close_reason    TEXT,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT,
    reported_at     TEXT NOT NULL,
    UNIQUE (username, broker_ticket)
);
CREATE INDEX IF NOT EXISTS ix_ea_fills_username
    ON ea_fills(username, opened_at DESC);
"""


@dataclass(frozen=True)
class EAFill:
    id: int
    username: str
    broker_ticket: int
    master_trade_id: int | None
    symbol: str
    side: str
    lot_size: float
    entry_price: float
    exit_price: float | None
    stop_loss: float | None
    take_profit: float | None
    pnl: float | None
    strategy: str | None
    status: str
    close_reason: str | None
    opened_at: datetime
    closed_at: datetime | None
    reported_at: datetime

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "broker_ticket": self.broker_ticket,
            "master_trade_id": self.master_trade_id,
            "symbol": self.symbol,
            "side": self.side,
            "lot_size": self.lot_size,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "pnl": self.pnl,
            "strategy": self.strategy,
            "status": self.status,
            "close_reason": self.close_reason,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }


class EAFillStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def upsert(
        self,
        username: str,
        *,
        broker_ticket: int,
        symbol: str,
        side: str,
        lot_size: float,
        entry_price: float,
        opened_at: datetime,
        status: str,
        master_trade_id: int | None = None,
        exit_price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        pnl: float | None = None,
        strategy: str | None = None,
        close_reason: str | None = None,
        closed_at: datetime | None = None,
        now: datetime | None = None,
    ) -> None:
        """Insert a new fill or update an existing one (by ticket).

        OPEN events typically come first with no exit/pnl/closed_at; the
        matching CLOSE event later supplies those. The unique
        (username, broker_ticket) constraint lets the EA retry safely —
        a re-sent OPEN won't duplicate, and a CLOSE arriving without a
        prior OPEN (EA started after the trade opened) still inserts the
        full row.
        """
        now = now or datetime.now(timezone.utc)
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO ea_fills (
                    username, broker_ticket, master_trade_id, symbol, side,
                    lot_size, entry_price, exit_price, stop_loss, take_profit,
                    pnl, strategy, status, close_reason, opened_at, closed_at,
                    reported_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (username, broker_ticket) DO UPDATE SET
                    master_trade_id = COALESCE(excluded.master_trade_id, ea_fills.master_trade_id),
                    exit_price      = COALESCE(excluded.exit_price,      ea_fills.exit_price),
                    stop_loss       = COALESCE(excluded.stop_loss,       ea_fills.stop_loss),
                    take_profit     = COALESCE(excluded.take_profit,     ea_fills.take_profit),
                    pnl             = COALESCE(excluded.pnl,             ea_fills.pnl),
                    strategy        = COALESCE(excluded.strategy,        ea_fills.strategy),
                    status          = excluded.status,
                    close_reason    = COALESCE(excluded.close_reason,    ea_fills.close_reason),
                    closed_at       = COALESCE(excluded.closed_at,       ea_fills.closed_at),
                    reported_at     = excluded.reported_at
                """,
                (
                    username, broker_ticket, master_trade_id, symbol, side,
                    lot_size, entry_price, exit_price, stop_loss, take_profit,
                    pnl, strategy, status, close_reason,
                    opened_at.isoformat(),
                    closed_at.isoformat() if closed_at else None,
                    now.isoformat(),
                ),
            )

    def recent(
        self,
        username: str,
        *,
        limit: int = 20,
        since_iso: str | None = None,
    ) -> list[EAFill]:
        """Most recent fills for this user, newest first.

        ``since_iso`` clips to fills opened on/after that timestamp —
        mirrors the journal.recent() filter so the EA-account
        ``first_seen_at`` cutoff still applies if callers want it.
        """
        limit = max(1, min(int(limit), 500))
        params: list = [username]
        clause = "username = ?"
        if since_iso:
            clause += " AND opened_at >= ?"
            params.append(since_iso)
        params.append(limit)
        with self._conn() as c:
            rows = c.execute(
                f"""
                SELECT id, username, broker_ticket, master_trade_id, symbol,
                       side, lot_size, entry_price, exit_price, stop_loss,
                       take_profit, pnl, strategy, status, close_reason,
                       opened_at, closed_at, reported_at
                FROM ea_fills
                WHERE {clause}
                ORDER BY COALESCE(closed_at, opened_at) DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        out: list[EAFill] = []
        for r in rows:
            out.append(EAFill(
                id=int(r["id"]),
                username=r["username"],
                broker_ticket=int(r["broker_ticket"]),
                master_trade_id=(int(r["master_trade_id"]) if r["master_trade_id"] is not None else None),
                symbol=r["symbol"],
                side=r["side"],
                lot_size=float(r["lot_size"]),
                entry_price=float(r["entry_price"]),
                exit_price=(float(r["exit_price"]) if r["exit_price"] is not None else None),
                stop_loss=(float(r["stop_loss"]) if r["stop_loss"] is not None else None),
                take_profit=(float(r["take_profit"]) if r["take_profit"] is not None else None),
                pnl=(float(r["pnl"]) if r["pnl"] is not None else None),
                strategy=r["strategy"],
                status=r["status"],
                close_reason=r["close_reason"],
                opened_at=datetime.fromisoformat(r["opened_at"]),
                closed_at=(datetime.fromisoformat(r["closed_at"]) if r["closed_at"] else None),
                reported_at=datetime.fromisoformat(r["reported_at"]),
            ))
        return out

    def count_open(self, username: str, *, since_iso: str | None = None) -> int:
        """Number of OPEN fills for this user (open positions on their MT5)."""
        params: list = [username]
        clause = "username = ? AND status = 'OPEN'"
        if since_iso:
            clause += " AND opened_at >= ?"
            params.append(since_iso)
        with self._conn() as c:
            row = c.execute(
                f"SELECT COUNT(*) AS n FROM ea_fills WHERE {clause}",
                tuple(params),
            ).fetchone()
        return int(row["n"]) if row else 0
