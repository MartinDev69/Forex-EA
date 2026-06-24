"""Shared SQLite connection factory.

Every store in the codebase talks to SQLite, and in production several do so
from different processes at once — the trading bot writes trades/fills while
the API serves dashboard reads. Two settings make that safe and are easy to
forget on a per-connection basis:

- ``journal_mode=WAL`` lets one writer and many readers run concurrently
  instead of locking each other out (the rollback-journal default blocks all
  readers for the duration of a write). WAL is a persistent, DB-level setting,
  but we (re)assert it on every connect so a freshly created DB file picks it
  up from its very first open.
- ``busy_timeout`` makes a blocked connection wait for the write lock instead
  of immediately raising ``sqlite3.OperationalError: database is locked``.
  Python's ``connect(timeout=...)`` already maps to this, but we also issue the
  PRAGMA explicitly so the intent is obvious and survives connections opened
  with a different timeout.

Use ``connect()`` from here instead of ``sqlite3.connect`` directly so every
store gets identical, correct locking behaviour.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# Seconds a blocked connection waits for the write lock before giving up.
_BUSY_TIMEOUT_S = 5.0


def connect(db_path: str | Path, *, timeout: float = _BUSY_TIMEOUT_S) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + a sane busy timeout applied."""
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
