"""Watchdog tick — runs once, exits.

Wired into Windows Task Scheduler to run every 60s. Reads heartbeat +
broker_status from data/trades.db, decides whether to restart the bot
service or kill the MT5 terminal, then exits.

Idempotent: cooldown logic in Watchdog._in_cooldown stops the same action
from firing on consecutive ticks.

Usage (Windows VPS, run via scheduled task — see deploy/watchdog-install.ps1):
    python scripts/watchdog.py

Manual one-off (any platform — actions degrade to "log only" off-Windows):
    python scripts/watchdog.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import platform
import subprocess
import sys
from pathlib import Path

# Allow `python scripts/watchdog.py` from the repo root without `-m`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.broker_status import BrokerStatusStore
from src.watchdog import HeartbeatStore, Watchdog, WatchdogConfig

log = logging.getLogger("watchdog")

DB_PATH = Path("data/trades.db")
BOT_SERVICE = "ForexEABot"
MT5_PROCESS_NAMES = ("terminal64.exe", "terminal.exe")


def _restart_bot_service(dry_run: bool) -> tuple[bool, str]:
    if dry_run or platform.system() != "Windows":
        return True, "dry-run / non-Windows: would have restarted ForexEABot"
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Restart-Service -Name {BOT_SERVICE} -Force"],
            check=True, capture_output=True, text=True, timeout=60,
        )
        return True, f"Restart-Service {BOT_SERVICE} succeeded"
    except subprocess.CalledProcessError as exc:
        return False, f"Restart-Service failed: {exc.stderr.strip() or exc.stdout.strip()}"
    except subprocess.TimeoutExpired:
        return False, "Restart-Service timed out after 60s"


def _recycle_mt5(dry_run: bool) -> tuple[bool, str]:
    """Kill the MT5 terminal so the bot's next reconnect creates a fresh
    session. Don't restart the bot here — _restart_bot_service does that
    on its own track if the heartbeat goes stale afterward.
    """
    if dry_run or platform.system() != "Windows":
        return True, "dry-run / non-Windows: would have killed MT5 terminal"
    killed = []
    for name in MT5_PROCESS_NAMES:
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/IM", name],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                killed.append(name)
        except subprocess.TimeoutExpired:
            return False, f"taskkill {name} timed out"
    if killed:
        # Bot will reinitialize MT5 on next tick (or NSSM will restart it).
        return True, f"killed MT5 processes: {', '.join(killed)}"
    return True, "no MT5 process found running — already gone"


def main() -> int:
    parser = argparse.ArgumentParser(description="Forex-EA watchdog tick")
    parser.add_argument("--dry-run", action="store_true",
                        help="Decide what to do, log it, but don't actually restart anything.")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [watchdog] %(levelname)s %(message)s",
    )

    if not args.db.exists():
        log.warning("DB %s missing — bot has never run. Nothing to watch.", args.db)
        return 0

    hb = HeartbeatStore(args.db)
    bs = BrokerStatusStore(args.db)
    wd = Watchdog(
        db_path=args.db,
        heartbeat_store=hb,
        broker_status_store=bs,
        restart_bot_cb=lambda: _restart_bot_service(args.dry_run),
        recycle_mt5_cb=lambda: _recycle_mt5(args.dry_run),
        config=WatchdogConfig.from_env(),
    )

    report = wd.run_once()
    if report.action.value == "none":
        log.debug("no action: %s", report.reason)
    else:
        level = log.info if report.success else log.error
        level("action=%s success=%s reason=%s detail=%s",
              report.action.value, report.success, report.reason, report.detail)
    return 0 if report.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
