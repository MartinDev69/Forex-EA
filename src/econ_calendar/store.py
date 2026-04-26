"""SQLite cache of economic-calendar events.

Writes are rare (provider refresh every ~30 min). Reads are hot — the bot
checks blackout status every time a signal fires, so indexed queries on
(currency, event_time) matter more than insert speed.

Times are stored as ISO-8601 UTC strings for readability in sqlite CLI
debugging. Comparisons still work because ISO-8601 UTC strings sort
lexicographically in the same order as the timestamps they represent.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_events (
    event_time TEXT NOT NULL,        -- ISO-8601 UTC
    currency   TEXT NOT NULL,        -- "USD", "EUR", ...
    impact     TEXT NOT NULL,        -- "high" | "medium" | "low"
    title      TEXT NOT NULL,        -- "Non-Farm Payrolls"
    actual     TEXT,
    forecast   TEXT,
    previous   TEXT,
    source     TEXT NOT NULL DEFAULT 'forexfactory',
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (event_time, currency, title)
);
CREATE INDEX IF NOT EXISTS idx_calendar_time     ON calendar_events(event_time);
CREATE INDEX IF NOT EXISTS idx_calendar_ccy_time ON calendar_events(currency, event_time);
CREATE INDEX IF NOT EXISTS idx_calendar_impact   ON calendar_events(impact, event_time);
"""

_VALID_IMPACTS = frozenset({"high", "medium", "low"})


@dataclass(frozen=True)
class CalendarEvent:
    event_time: datetime            # tz-aware UTC
    currency: str
    impact: str                     # lowercase: "high" | "medium" | "low"
    title: str
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    source: str = "forexfactory"

    def __post_init__(self) -> None:
        # Frozen dataclasses need __post_init__ validation via object.__setattr__
        # but here we only validate — mutation would defeat the frozen guarantee.
        if self.event_time.tzinfo is None:
            raise ValueError("event_time must be timezone-aware")
        if self.impact not in _VALID_IMPACTS:
            raise ValueError(f"impact must be one of {sorted(_VALID_IMPACTS)}, got {self.impact!r}")
        if not self.currency or len(self.currency) > 5:
            raise ValueError(f"currency code looks wrong: {self.currency!r}")

    def minutes_until(self, now: datetime) -> float:
        delta = self.event_time - now
        return delta.total_seconds() / 60.0

    def to_dict(self) -> dict:
        return {
            "event_time": self.event_time.astimezone(timezone.utc).isoformat(),
            "currency": self.currency,
            "impact": self.impact,
            "title": self.title,
            "actual": self.actual,
            "forecast": self.forecast,
            "previous": self.previous,
            "source": self.source,
        }


class EventStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------- writes ----------

    def upsert_many(self, events: Iterable[CalendarEvent]) -> int:
        """Insert or update. Returns count written."""
        fetched_at = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                e.event_time.astimezone(timezone.utc).isoformat(),
                e.currency,
                e.impact,
                e.title,
                e.actual,
                e.forecast,
                e.previous,
                e.source,
                fetched_at,
            )
            for e in events
        ]
        if not rows:
            return 0
        with self._conn() as c:
            c.executemany(
                """
                INSERT INTO calendar_events
                    (event_time, currency, impact, title, actual, forecast, previous, source, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_time, currency, title) DO UPDATE SET
                    impact     = excluded.impact,
                    actual     = excluded.actual,
                    forecast   = excluded.forecast,
                    previous   = excluded.previous,
                    source     = excluded.source,
                    fetched_at = excluded.fetched_at
                """,
                rows,
            )
        return len(rows)

    def purge_before(self, cutoff: datetime) -> int:
        """Drop old events so the table doesn't grow unbounded."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM calendar_events WHERE event_time < ?",
                (cutoff.astimezone(timezone.utc).isoformat(),),
            )
        return cur.rowcount

    # ---------- reads ----------

    def events_in_window(
        self,
        currencies: Iterable[str],
        start: datetime,
        end: datetime,
        impacts: Iterable[str],
    ) -> list[CalendarEvent]:
        """Events for `currencies` whose event_time falls in [start, end] at
        the given impact levels. Returns earliest-first."""
        ccy = list(currencies)
        imp = list(impacts)
        if not ccy or not imp:
            return []
        with self._conn() as c:
            q = (
                "SELECT * FROM calendar_events "
                "WHERE event_time >= ? AND event_time <= ? "
                f"AND currency IN ({','.join('?' * len(ccy))}) "
                f"AND impact   IN ({','.join('?' * len(imp))}) "
                "ORDER BY event_time ASC"
            )
            params = [
                start.astimezone(timezone.utc).isoformat(),
                end.astimezone(timezone.utc).isoformat(),
                *ccy,
                *imp,
            ]
            rows = c.execute(q, params).fetchall()
        return [_row_to_event(r) for r in rows]

    def next_event(
        self,
        currencies: Iterable[str],
        after: datetime,
        impacts: Iterable[str],
    ) -> CalendarEvent | None:
        ccy = list(currencies)
        imp = list(impacts)
        if not ccy or not imp:
            return None
        with self._conn() as c:
            q = (
                "SELECT * FROM calendar_events "
                "WHERE event_time > ? "
                f"AND currency IN ({','.join('?' * len(ccy))}) "
                f"AND impact   IN ({','.join('?' * len(imp))}) "
                "ORDER BY event_time ASC LIMIT 1"
            )
            row = c.execute(
                q,
                [after.astimezone(timezone.utc).isoformat(), *ccy, *imp],
            ).fetchone()
        return _row_to_event(row) if row else None

    def count(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0])


def _row_to_event(row: sqlite3.Row) -> CalendarEvent:
    return CalendarEvent(
        event_time=datetime.fromisoformat(row["event_time"]),
        currency=row["currency"],
        impact=row["impact"],
        title=row["title"],
        actual=row["actual"],
        forecast=row["forecast"],
        previous=row["previous"],
        source=row["source"],
    )
