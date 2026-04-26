"""Watchdog decision logic.

`Watchdog.run_once()` reads heartbeat + broker status, decides what (if
anything) needs recycling, executes the action via injected callbacks, and
records the outcome in `watchdog_actions` so the dashboard can show recent
activity. Pure function in shape — side-effects are all callbacks, so tests
swap them for fakes.
"""
from __future__ import annotations

import enum
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.api.broker_status import BrokerStatusStore
from .heartbeat import Heartbeat, HeartbeatStore

log = logging.getLogger(__name__)

_ACTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchdog_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at    TEXT NOT NULL,
    action      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    success     INTEGER NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_watchdog_actions_taken
    ON watchdog_actions(taken_at DESC);
"""


class WatchdogAction(str, enum.Enum):
    NONE = "none"                  # everything healthy
    RESTART_BOT = "restart_bot"    # heartbeat stale — bot wedged or dead
    RECYCLE_MT5 = "recycle_mt5"    # broker disconnected too long, bot still ticking


@dataclass
class WatchdogConfig:
    bot_process_name: str = "bot"
    heartbeat_stale_s: int = 180          # 3 min — covers a few poll cycles
    broker_disconnect_s: int = 300        # 5 min before recycling MT5
    # Min seconds between repeat actions of the same kind. Stops a bouncing
    # service from getting hammered every watchdog tick.
    cooldown_s: int = 600

    @classmethod
    def from_env(cls, env: dict | None = None) -> "WatchdogConfig":
        e = env if env is not None else os.environ
        return cls(
            heartbeat_stale_s=int(e.get("WATCHDOG_HEARTBEAT_STALE_S", "180")),
            broker_disconnect_s=int(e.get("WATCHDOG_BROKER_DISCONNECT_S", "300")),
            cooldown_s=int(e.get("WATCHDOG_COOLDOWN_S", "600")),
        )


@dataclass
class WatchdogReport:
    action: WatchdogAction
    reason: str
    success: bool
    detail: str | None = None
    taken_at: datetime | None = None


class Watchdog:
    """Compose with injected callbacks so tests don't need real services.

    `restart_bot_cb` should return True on success. `recycle_mt5_cb` should
    kill the MT5 terminal process — the bot service restart that follows
    will reinitialize MT5 from MT5_PATH in .env.
    """

    def __init__(
        self,
        db_path: Path | str,
        heartbeat_store: HeartbeatStore,
        broker_status_store: BrokerStatusStore,
        restart_bot_cb: Callable[[], tuple[bool, str]],
        recycle_mt5_cb: Callable[[], tuple[bool, str]],
        config: WatchdogConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.hb = heartbeat_store
        self.broker = broker_status_store
        self.restart_bot_cb = restart_bot_cb
        self.recycle_mt5_cb = recycle_mt5_cb
        self.config = config or WatchdogConfig()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self._conn() as c:
            c.executescript(_ACTIONS_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def run_once(self) -> WatchdogReport:
        now = self.clock()
        hb = self.hb.read(self.config.bot_process_name)
        decision = self._decide(hb, now)

        if decision is None:
            report = WatchdogReport(
                action=WatchdogAction.NONE,
                reason="all healthy" if hb else "no heartbeat yet — bot may not have started",
                success=True,
                taken_at=now,
            )
            self._record(report)
            return report

        action, reason = decision

        if self._in_cooldown(action, now):
            report = WatchdogReport(
                action=WatchdogAction.NONE,
                reason=f"would have run {action.value} ({reason}) but in cooldown",
                success=True,
                taken_at=now,
            )
            self._record(report)
            return report

        cb = self.restart_bot_cb if action == WatchdogAction.RESTART_BOT else self.recycle_mt5_cb
        try:
            ok, detail = cb()
        except Exception as exc:
            log.exception("watchdog action %s raised", action.value)
            ok, detail = False, f"exception: {exc!r}"

        report = WatchdogReport(
            action=action, reason=reason, success=ok, detail=detail, taken_at=now,
        )
        self._record(report)
        return report

    def _decide(
        self, hb: Heartbeat | None, now: datetime
    ) -> tuple[WatchdogAction, str] | None:
        if hb is None:
            # No heartbeat row at all. Could be a fresh deploy. Don't act —
            # the cooldown logic alone wouldn't help; the bot literally hasn't
            # started yet. Operator will notice via /health.
            return None

        age = hb.age_seconds(now)
        if age >= self.config.heartbeat_stale_s:
            return (
                WatchdogAction.RESTART_BOT,
                f"heartbeat stale ({age:.0f}s ≥ {self.config.heartbeat_stale_s}s threshold)",
            )

        # Bot is ticking but maybe MT5 is dead. Read broker status.
        bs = self.broker.read()
        if bs is None or bs.connected:
            return None

        disconnect_age = (now - bs.updated_at).total_seconds()
        # Use whichever is older: how long ago broker_status was updated, or
        # how long ago we know it's been disconnected. We treat updated_at as
        # the confirmation timestamp — `connected=False` for >threshold means
        # the bot's been seeing failures that long.
        if disconnect_age >= self.config.broker_disconnect_s:
            return (
                WatchdogAction.RECYCLE_MT5,
                (
                    f"broker disconnected {disconnect_age:.0f}s "
                    f"(≥ {self.config.broker_disconnect_s}s); "
                    f"last_error={bs.last_error or 'unknown'}"
                ),
            )
        return None

    def _in_cooldown(self, action: WatchdogAction, now: datetime) -> bool:
        with self._conn() as c:
            row = c.execute(
                """
                SELECT taken_at FROM watchdog_actions
                WHERE action = ? AND success = 1
                ORDER BY id DESC LIMIT 1
                """,
                (action.value,),
            ).fetchone()
        if row is None:
            return False
        last = datetime.fromisoformat(row["taken_at"])
        return (now - last).total_seconds() < self.config.cooldown_s

    def _record(self, r: WatchdogReport) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO watchdog_actions (taken_at, action, reason, success, detail)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (r.taken_at or self.clock()).isoformat(),
                    r.action.value,
                    r.reason,
                    1 if r.success else 0,
                    r.detail,
                ),
            )

    def recent_actions(self, limit: int = 20) -> list[WatchdogReport]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT taken_at, action, reason, success, detail
                FROM watchdog_actions
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            WatchdogReport(
                action=WatchdogAction(r["action"]),
                reason=r["reason"],
                success=bool(r["success"]),
                detail=r["detail"],
                taken_at=datetime.fromisoformat(r["taken_at"]),
            )
            for r in rows
        ]
