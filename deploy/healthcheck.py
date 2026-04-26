"""Forex-EA health check.

Prints a human-readable report and exits non-zero if anything looks wrong.
Intended for:
  * ad-hoc diagnostics after a deploy (`python deploy/healthcheck.py`)
  * periodic monitoring via a scheduled task / cron
  * CI smoke test against a staging VPS

Checks (each can individually fail — overall exit code is non-zero if any do):
  * API /health reachable and returns ok
  * Journal SQLite file opens cleanly and has the expected schema
  * Most recent trade activity is younger than --max-trade-age-h (if any trades)
  * Log files exist and have been written to in the last --max-log-age-min
  * Free disk space at the repo root is above --min-free-gb

This module is imported by tests, so keep the check functions pure-ish
(return a HealthCheck namedtuple rather than calling sys.exit).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


@dataclass
class HealthCheck:
    name: str
    ok: bool
    detail: str


def check_api(url: str, timeout: float = 3.0) -> HealthCheck:
    try:
        import urllib.request  # stdlib — no httpx dependency required
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status != 200:
                return HealthCheck("api", False, f"{url} returned {resp.status}")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return HealthCheck("api", False, f"{url} returned non-JSON: {body[:80]}")
            if data.get("status") != "ok":
                return HealthCheck("api", False, f"unexpected /health body: {data}")
            return HealthCheck("api", True, f"{url} -> ok")
    except Exception as e:
        return HealthCheck("api", False, f"{url} unreachable: {e}")


def check_journal(db_path: Path) -> HealthCheck:
    if not db_path.exists():
        return HealthCheck("journal", False, f"{db_path} does not exist")
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute("SELECT COUNT(*) FROM trades")
        n = cur.fetchone()[0]
        conn.close()
    except sqlite3.Error as e:
        return HealthCheck("journal", False, f"sqlite error: {e}")
    return HealthCheck("journal", True, f"{n} trades in {db_path}")


def check_trade_recency(db_path: Path, max_age_h: float) -> HealthCheck:
    """A stale journal suggests the bot stopped trading — but we tolerate
    an empty journal (brand-new install) and only flag when there IS data and
    it's gone cold."""
    if not db_path.exists():
        return HealthCheck("trade_recency", False, f"{db_path} does not exist")
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.execute("SELECT MAX(opened_at) FROM trades")
        last_iso = cur.fetchone()[0]
        conn.close()
    except sqlite3.Error as e:
        return HealthCheck("trade_recency", False, f"sqlite error: {e}")
    if last_iso is None:
        return HealthCheck("trade_recency", True, "no trades yet (ok on a fresh install)")
    try:
        last_dt = datetime.fromisoformat(last_iso)
    except ValueError:
        return HealthCheck("trade_recency", False, f"unparseable opened_at: {last_iso}")
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - last_dt
    if age > timedelta(hours=max_age_h):
        return HealthCheck("trade_recency", False,
                           f"last trade {age.total_seconds()/3600:.1f}h ago (> {max_age_h}h)")
    return HealthCheck("trade_recency", True,
                       f"last trade {age.total_seconds()/3600:.1f}h ago")


def check_log_freshness(log_dir: Path, max_age_min: float) -> HealthCheck:
    """Fresh log writes are a cheap liveness signal — the bot writes a
    heartbeat line every tick."""
    if not log_dir.exists():
        return HealthCheck("logs", False, f"{log_dir} does not exist")
    log_files = [p for p in log_dir.iterdir() if p.is_file() and p.suffix == ".log"]
    if not log_files:
        return HealthCheck("logs", False, f"no *.log files in {log_dir}")
    latest = max(log_files, key=lambda p: p.stat().st_mtime)
    age_s = time.time() - latest.stat().st_mtime
    if age_s > max_age_min * 60:
        return HealthCheck("logs", False,
                           f"latest log ({latest.name}) last written {age_s/60:.1f}m ago")
    return HealthCheck("logs", True, f"{latest.name} written {age_s/60:.1f}m ago")


def check_disk_free(path: Path, min_free_gb: float) -> HealthCheck:
    usage = shutil.disk_usage(path)
    free_gb = usage.free / (1024**3)
    ok = free_gb >= min_free_gb
    return HealthCheck("disk", ok,
                       f"{free_gb:.1f} GB free at {path} (min {min_free_gb} GB)")


def run_all(
    repo_root: Path,
    api_url: str,
    max_trade_age_h: float,
    max_log_age_min: float,
    min_free_gb: float,
) -> list[HealthCheck]:
    db = repo_root / "data" / "trades.db"
    logs = repo_root / "logs"
    return [
        check_api(api_url),
        check_journal(db),
        check_trade_recency(db, max_trade_age_h),
        check_log_freshness(logs, max_log_age_min),
        check_disk_free(repo_root, min_free_gb),
    ]


def render(checks: Iterable[HealthCheck]) -> str:
    lines = []
    for c in checks:
        flag = "OK  " if c.ok else "FAIL"
        lines.append(f"[{flag}] {c.name:15s} {c.detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path,
                        default=Path(__file__).resolve().parents[1])
    parser.add_argument("--api-url", default="http://127.0.0.1:8000/health")
    parser.add_argument("--max-trade-age-h", type=float, default=24.0)
    parser.add_argument("--max-log-age-min", type=float, default=10.0)
    parser.add_argument("--min-free-gb", type=float, default=1.0)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    checks = run_all(
        repo_root=args.repo_root,
        api_url=args.api_url,
        max_trade_age_h=args.max_trade_age_h,
        max_log_age_min=args.max_log_age_min,
        min_free_gb=args.min_free_gb,
    )
    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        print(render(checks))
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
