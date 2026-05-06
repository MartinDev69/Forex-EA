"""Pending-order snapshot — written by the bot, read by the API.

Pending orders (buy_limit, sell_limit, buy_stop, sell_stop) are MT5
entities that haven't filled yet, distinct from the executed positions
the journal tracks. The bot refreshes this table on every tick by
overwriting it with whatever `mt5.orders_get()` returns; orders that
fill or get cancelled drop off naturally.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_orders (
    ticket          INTEGER PRIMARY KEY,
    symbol          TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    price           REAL NOT NULL,
    volume          REAL NOT NULL,
    sl              REAL,
    tp              REAL,
    comment         TEXT,
    placed_at       TEXT NOT NULL,
    refreshed_at    TEXT NOT NULL
);
"""


@dataclass
class PendingOrder:
    ticket: int
    symbol: str
    order_type: str  # buy_limit | sell_limit | buy_stop | sell_stop | unknown
    price: float
    volume: float
    sl: float | None
    tp: float | None
    comment: str | None
    placed_at: datetime


class PendingOrderStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def replace_all(self, orders: list[PendingOrder]) -> None:
        """Snapshot replace — anything not in `orders` is dropped."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as c:
            c.execute("DELETE FROM pending_orders")
            for o in orders:
                c.execute(
                    """
                    INSERT INTO pending_orders (ticket, symbol, order_type, price,
                        volume, sl, tp, comment, placed_at, refreshed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        o.ticket, o.symbol, o.order_type, o.price, o.volume,
                        o.sl, o.tp, o.comment, o.placed_at.isoformat(), now,
                    ),
                )

    def read(self) -> list[PendingOrder]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM pending_orders ORDER BY placed_at DESC"
            ).fetchall()
        return [
            PendingOrder(
                ticket=row["ticket"],
                symbol=row["symbol"],
                order_type=row["order_type"],
                price=row["price"],
                volume=row["volume"],
                sl=row["sl"],
                tp=row["tp"],
                comment=row["comment"],
                placed_at=datetime.fromisoformat(row["placed_at"]),
            )
            for row in rows
        ]
