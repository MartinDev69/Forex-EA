"""Print the bot's heartbeat as `<tick_count> <age_seconds> <pid>`.

Exists as a file rather than an inline `python -c` string because quoting a SQL
statement through PowerShell -> cmd -> python mangles the embedded quotes; the
inline version in service-control.ps1 failed with a SyntaxError at the shell
boundary. Used by service-control.ps1 to tell "started" from "actually running".

Exit code 1 if there is no heartbeat row at all.
"""
from __future__ import annotations

import datetime
import sqlite3
import sys
from pathlib import Path

db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/trades.db")
process = sys.argv[2] if len(sys.argv) > 2 else "bot"

conn = sqlite3.connect(str(db))
try:
    row = conn.execute(
        "SELECT tick_count, last_tick_at, pid FROM watchdog_heartbeat "
        "WHERE process_name = ?",
        (process,),
    ).fetchone()
finally:
    conn.close()

if row is None:
    print("no heartbeat row", file=sys.stderr)
    raise SystemExit(1)

ticks, last_tick_at, pid = row
age = (
    datetime.datetime.now(datetime.timezone.utc)
    - datetime.datetime.fromisoformat(last_tick_at)
).total_seconds()
print(f"{ticks} {age:.1f} {pid}")
