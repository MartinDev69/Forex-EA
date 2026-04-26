"""Self-healing watchdog.

Detects two failure modes that NSSM's restart-on-crash misses:
  1. Bot process is alive but tick loop is wedged (no progress for N seconds).
  2. MT5 terminal has hung — bot keeps polling but every call returns errors.

The bot writes a per-tick heartbeat to SQLite. An external watchdog process
(scripts/watchdog.py, run via Windows scheduled task) reads it, compares to
broker_status, and decides whether to restart the bot service or recycle
the MT5 terminal.
"""
from .heartbeat import HeartbeatStore, Heartbeat
from .watchdog import Watchdog, WatchdogAction, WatchdogConfig, WatchdogReport

__all__ = [
    "Heartbeat",
    "HeartbeatStore",
    "Watchdog",
    "WatchdogAction",
    "WatchdogConfig",
    "WatchdogReport",
]
