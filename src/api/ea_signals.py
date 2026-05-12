"""Signal feed for the AntiGreedCopier MetaTrader EA.

The EA polls ``/signals/feed?since=<iso>`` every few seconds with its
user's API key. We hit the trade journal directly and synthesise OPEN
and CLOSE events from the timestamps — no separate event log needed.

Each event has a stable identity ``(event_type, trade_id)`` so the EA
can dedup if it ever replays a window. ``ts`` is the canonical
ordering key the EA passes back as ``since`` on the next poll.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SignalEvent:
    event_type: str        # 'OPEN' | 'CLOSE'
    trade_id: int
    ts: str                # ISO-8601 — also the ordering key
    symbol: str
    side: str              # 'BUY' | 'SELL'
    lot_size: float
    price: float
    stop_loss: float | None
    take_profit: float | None
    strategy: str | None
    broker_ticket: int | None


class SignalFeed:
    """Read-only view over the trade journal that emits OPEN/CLOSE
    events for the copy-trading EA.

    Doesn't own a connection — opens one per call so it stays
    threadsafe alongside the main journal writer in the bot process.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def events_since(self, since_iso: str | None, limit: int = 100) -> list[SignalEvent]:
        """All OPEN/CLOSE events with a timestamp strictly newer than
        ``since_iso``, oldest first. Pass None on first poll to get
        the most recent ``limit`` events.
        """
        # First-time pollers get the recent tail rather than every
        # historical trade — replaying years of trades the moment a
        # new EA boots up would flood the user's account.
        if not since_iso:
            with self._conn() as c:
                row = c.execute(
                    "SELECT MAX(ts) AS mx FROM ("
                    "  SELECT opened_at AS ts FROM trades "
                    "  UNION ALL "
                    "  SELECT closed_at AS ts FROM trades WHERE closed_at IS NOT NULL"
                    ")"
                ).fetchone()
            since_iso = row["mx"] if row and row["mx"] else "1970-01-01T00:00:00+00:00"
            # Return zero events on cold-start so the EA's first poll
            # bookmarks "now" without firing trades for the recent past.
            return []

        with self._conn() as c:
            rows = c.execute(
                """
                SELECT 'OPEN' AS event_type, id AS trade_id,
                       opened_at AS ts, symbol, side, lot_size,
                       entry_price AS price, stop_loss, take_profit,
                       strategy, broker_ticket
                FROM trades
                WHERE opened_at > ?
                UNION ALL
                SELECT 'CLOSE' AS event_type, id AS trade_id,
                       closed_at AS ts, symbol, side, lot_size,
                       exit_price AS price, NULL, NULL,
                       strategy, broker_ticket
                FROM trades
                WHERE closed_at IS NOT NULL AND closed_at > ?
                ORDER BY ts ASC
                LIMIT ?
                """,
                (since_iso, since_iso, limit),
            ).fetchall()
        out: list[SignalEvent] = []
        for r in rows:
            out.append(SignalEvent(
                event_type=r["event_type"],
                trade_id=int(r["trade_id"]),
                ts=r["ts"],
                symbol=r["symbol"],
                side=r["side"],
                lot_size=float(r["lot_size"] or 0.0),
                price=float(r["price"] or 0.0),
                stop_loss=float(r["stop_loss"]) if r["stop_loss"] else None,
                take_profit=float(r["take_profit"]) if r["take_profit"] else None,
                strategy=r["strategy"],
                broker_ticket=int(r["broker_ticket"]) if r["broker_ticket"] else None,
            ))
        return out
