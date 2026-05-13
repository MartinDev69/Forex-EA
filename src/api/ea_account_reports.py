"""Per-operator account snapshots reported by the AntiGreedCopier EA.

Copy-trading users don't run MT5 on this server, so the API can't see
their broker account directly. The EA POSTs a snapshot every 60s with
balance / equity / margin / login / server / broker / currency. The
dashboard reads from here when the caller is a non-admin so they see
their own account instead of the admin's.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ea_account_reports (
    username     TEXT PRIMARY KEY,
    balance      REAL,
    equity       REAL,
    margin       REAL,
    free_margin  REAL,
    login        INTEGER,
    server       TEXT,
    broker       TEXT,
    currency     TEXT,
    updated_at   TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class EAAccountReport:
    username: str
    balance: float | None
    equity: float | None
    margin: float | None
    free_margin: float | None
    login: int | None
    server: str | None
    broker: str | None
    currency: str | None
    updated_at: datetime


class EAAccountReportStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
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
        balance: float | None,
        equity: float | None,
        margin: float | None,
        free_margin: float | None,
        login: int | None,
        server: str | None,
        broker: str | None,
        currency: str | None,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        with self._conn() as c:
            c.execute(
                "INSERT INTO ea_account_reports "
                "(username, balance, equity, margin, free_margin, login, "
                " server, broker, currency, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET "
                "  balance = excluded.balance, "
                "  equity = excluded.equity, "
                "  margin = excluded.margin, "
                "  free_margin = excluded.free_margin, "
                "  login = excluded.login, "
                "  server = excluded.server, "
                "  broker = excluded.broker, "
                "  currency = excluded.currency, "
                "  updated_at = excluded.updated_at",
                (
                    username, balance, equity, margin, free_margin,
                    login, server, broker, currency, now.isoformat(),
                ),
            )

    def get(self, username: str) -> EAAccountReport | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT username, balance, equity, margin, free_margin, "
                "login, server, broker, currency, updated_at "
                "FROM ea_account_reports WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return EAAccountReport(
            username=row["username"],
            balance=row["balance"],
            equity=row["equity"],
            margin=row["margin"],
            free_margin=row["free_margin"],
            login=row["login"],
            server=row["server"],
            broker=row["broker"],
            currency=row["currency"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
