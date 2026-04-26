"""Tests for the self-healing watchdog: heartbeat store, decision logic,
cooldowns, and the API surface.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.broker_status import BrokerStatusStore
from src.watchdog import (
    HeartbeatStore,
    Watchdog,
    WatchdogAction,
    WatchdogConfig,
)


def _now() -> datetime:
    return datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)


def _make_wd(tmp_path: Path, *, restarts=None, recycles=None, clock=None, config=None):
    """Build a Watchdog with capturing fakes for the action callbacks."""
    db = tmp_path / "trades.db"
    hb = HeartbeatStore(db)
    bs = BrokerStatusStore(db)
    restarts = restarts if restarts is not None else []
    recycles = recycles if recycles is not None else []

    def _restart() -> tuple[bool, str]:
        restarts.append("called")
        return True, "fake restart"

    def _recycle() -> tuple[bool, str]:
        recycles.append("called")
        return True, "fake recycle"

    return Watchdog(
        db_path=db,
        heartbeat_store=hb,
        broker_status_store=bs,
        restart_bot_cb=_restart,
        recycle_mt5_cb=_recycle,
        config=config or WatchdogConfig(heartbeat_stale_s=180, broker_disconnect_s=300, cooldown_s=600),
        clock=clock or _now,
    ), hb, bs, restarts, recycles


# ---------------------------------------------------------------- heartbeat


def test_heartbeat_roundtrip(tmp_path):
    hb = HeartbeatStore(tmp_path / "trades.db")
    hb.write(process_name="bot", tick_count=5)
    got = hb.read("bot")
    assert got is not None
    assert got.process_name == "bot"
    assert got.tick_count == 5
    assert got.last_error is None
    assert got.pid is not None  # filled in from os.getpid()


def test_heartbeat_overwrite_and_age(tmp_path):
    hb = HeartbeatStore(tmp_path / "trades.db")
    early = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    hb.write(process_name="bot", tick_count=1, now=early)
    later = early + timedelta(seconds=120)
    hb.write(process_name="bot", tick_count=2, last_error="boom", now=later)
    got = hb.read("bot")
    assert got.tick_count == 2
    assert got.last_error == "boom"
    # age relative to a still-later instant
    assert got.age_seconds(later + timedelta(seconds=30)) == 30.0


def test_heartbeat_missing_returns_none(tmp_path):
    hb = HeartbeatStore(tmp_path / "trades.db")
    assert hb.read("bot") is None


def test_heartbeat_all_returns_each_process(tmp_path):
    hb = HeartbeatStore(tmp_path / "trades.db")
    hb.write(process_name="bot", tick_count=1)
    hb.write(process_name="api", tick_count=10)
    rows = hb.all()
    names = {r.process_name for r in rows}
    assert names == {"bot", "api"}


# ---------------------------------------------------------------- decisions


def test_no_heartbeat_yields_no_action(tmp_path):
    wd, hb, bs, restarts, recycles = _make_wd(tmp_path)
    report = wd.run_once()
    assert report.action == WatchdogAction.NONE
    assert restarts == [] and recycles == []


def test_fresh_heartbeat_and_connected_broker_does_nothing(tmp_path):
    wd, hb, bs, restarts, recycles = _make_wd(tmp_path)
    hb.write(process_name="bot", tick_count=1, now=_now() - timedelta(seconds=10))
    bs.write(connected=True, broker="exness")
    report = wd.run_once()
    assert report.action == WatchdogAction.NONE
    assert restarts == [] and recycles == []


def test_stale_heartbeat_triggers_bot_restart(tmp_path):
    wd, hb, bs, restarts, recycles = _make_wd(tmp_path)
    hb.write(process_name="bot", tick_count=1, now=_now() - timedelta(seconds=240))
    report = wd.run_once()
    assert report.action == WatchdogAction.RESTART_BOT
    assert report.success
    assert restarts == ["called"]
    assert "stale" in report.reason


def test_disconnected_broker_with_fresh_heartbeat_recycles_mt5(tmp_path):
    wd, hb, bs, restarts, recycles = _make_wd(tmp_path)
    # Bot is ticking fine — heartbeat is fresh.
    hb.write(process_name="bot", tick_count=42, now=_now() - timedelta(seconds=5))
    # Broker has been disconnected for longer than the threshold.
    old = (_now() - timedelta(seconds=600)).isoformat()
    # Insert directly so we can backdate updated_at.
    import sqlite3
    with sqlite3.connect(bs.db_path) as c:
        c.execute(
            """INSERT OR REPLACE INTO broker_status
               (id, connected, broker, server, login, account_info, last_error, updated_at)
               VALUES (1, 0, 'exness', 'Exness-MT5Trial', 12345, NULL, 'reconnect failed', ?)""",
            (old,),
        )
    report = wd.run_once()
    assert report.action == WatchdogAction.RECYCLE_MT5
    assert recycles == ["called"]
    assert "reconnect failed" in report.reason


def test_briefly_disconnected_broker_does_not_recycle(tmp_path):
    wd, hb, bs, restarts, recycles = _make_wd(tmp_path)
    hb.write(process_name="bot", tick_count=42, now=_now() - timedelta(seconds=5))
    # Disconnected for only 60s — well under the 300s threshold.
    bs.write(connected=False, broker="exness", last_error="transient")
    # broker_status uses now() in write(); patch by re-writing through sqlite to backdate.
    import sqlite3
    backdated = (_now() - timedelta(seconds=60)).isoformat()
    with sqlite3.connect(bs.db_path) as c:
        c.execute("UPDATE broker_status SET updated_at = ? WHERE id = 1", (backdated,))
    report = wd.run_once()
    assert report.action == WatchdogAction.NONE
    assert recycles == []


def test_cooldown_blocks_repeat_action(tmp_path):
    clock_state = {"now": _now()}

    def clock():
        return clock_state["now"]

    wd, hb, bs, restarts, recycles = _make_wd(tmp_path, clock=clock)
    hb.write(process_name="bot", tick_count=1, now=clock_state["now"] - timedelta(seconds=240))
    # First run restarts.
    r1 = wd.run_once()
    assert r1.action == WatchdogAction.RESTART_BOT
    # Heartbeat still stale (we haven't actually restarted anything in tests).
    # Advance clock by 60s — well under the 600s cooldown.
    clock_state["now"] += timedelta(seconds=60)
    r2 = wd.run_once()
    assert r2.action == WatchdogAction.NONE
    assert "cooldown" in r2.reason
    assert restarts == ["called"]  # no second restart


def test_cooldown_clears_after_window(tmp_path):
    clock_state = {"now": _now()}

    def clock():
        return clock_state["now"]

    wd, hb, bs, restarts, recycles = _make_wd(tmp_path, clock=clock)
    hb.write(process_name="bot", tick_count=1, now=clock_state["now"] - timedelta(seconds=240))
    wd.run_once()
    # Advance past the 600s cooldown.
    clock_state["now"] += timedelta(seconds=601)
    # Heartbeat is now even staler. Re-run.
    r2 = wd.run_once()
    assert r2.action == WatchdogAction.RESTART_BOT
    assert restarts == ["called", "called"]


def test_recent_actions_are_recorded_for_dashboard(tmp_path):
    wd, hb, bs, restarts, recycles = _make_wd(tmp_path)
    hb.write(process_name="bot", tick_count=1, now=_now() - timedelta(seconds=240))
    wd.run_once()
    actions = wd.recent_actions(limit=5)
    assert len(actions) == 1
    assert actions[0].action == WatchdogAction.RESTART_BOT
    assert actions[0].success


def test_action_callback_failure_recorded_as_unsuccessful(tmp_path):
    db = tmp_path / "trades.db"
    hb = HeartbeatStore(db)
    bs = BrokerStatusStore(db)

    def _bad_restart():
        return False, "Restart-Service: access denied"

    wd = Watchdog(
        db_path=db,
        heartbeat_store=hb,
        broker_status_store=bs,
        restart_bot_cb=_bad_restart,
        recycle_mt5_cb=lambda: (True, "n/a"),
        config=WatchdogConfig(heartbeat_stale_s=180, broker_disconnect_s=300, cooldown_s=600),
        clock=_now,
    )
    hb.write(process_name="bot", tick_count=1, now=_now() - timedelta(seconds=240))
    report = wd.run_once()
    assert report.success is False
    assert "access denied" in report.detail


def test_action_callback_exception_is_caught(tmp_path):
    db = tmp_path / "trades.db"
    hb = HeartbeatStore(db)
    bs = BrokerStatusStore(db)

    def _boom():
        raise RuntimeError("nssm not on PATH")

    wd = Watchdog(
        db_path=db,
        heartbeat_store=hb,
        broker_status_store=bs,
        restart_bot_cb=_boom,
        recycle_mt5_cb=lambda: (True, "n/a"),
        config=WatchdogConfig(heartbeat_stale_s=180, broker_disconnect_s=300, cooldown_s=600),
        clock=_now,
    )
    hb.write(process_name="bot", tick_count=1, now=_now() - timedelta(seconds=240))
    report = wd.run_once()
    assert report.success is False
    assert "nssm" in report.detail


def test_watchdogconfig_from_env_reads_overrides(monkeypatch):
    monkeypatch.setenv("WATCHDOG_HEARTBEAT_STALE_S", "90")
    monkeypatch.setenv("WATCHDOG_BROKER_DISCONNECT_S", "120")
    monkeypatch.setenv("WATCHDOG_COOLDOWN_S", "30")
    cfg = WatchdogConfig.from_env()
    assert cfg.heartbeat_stale_s == 90
    assert cfg.broker_disconnect_s == 120
    assert cfg.cooldown_s == 30


# ---------------------------------------------------------------- API


def test_watchdog_endpoint_exposes_heartbeats_and_actions(tmp_path, monkeypatch):
    """Override the module-level stores so /watchdog reads from a tmp DB."""
    monkeypatch.setenv("AUTH_SECRET", "x" * 64)
    db = tmp_path / "trades.db"

    hb = HeartbeatStore(db)
    hb.write(process_name="bot", tick_count=99)
    bs = BrokerStatusStore(db)
    bs.write(connected=True, broker="exness")

    # Record one watchdog action against the tmp DB so the endpoint has
    # something to surface.
    Watchdog(
        db_path=db,
        heartbeat_store=hb,
        broker_status_store=bs,
        restart_bot_cb=lambda: (True, "n/a"),
        recycle_mt5_cb=lambda: (True, "n/a"),
        config=WatchdogConfig(heartbeat_stale_s=180, broker_disconnect_s=300, cooldown_s=600),
    ).run_once()

    from src.api import auth as auth_module
    from src.api import server as server_module
    monkeypatch.setattr(server_module, "_DB", db)
    monkeypatch.setattr(server_module, "heartbeat_store", hb)
    monkeypatch.setattr(server_module, "broker_status_store", bs)
    server_module.app.dependency_overrides[auth_module.current_user] = lambda: {
        "username": "test", "role": "admin",
    }
    try:
        client = TestClient(server_module.app)
        r = client.get("/watchdog")
        assert r.status_code == 200
        payload = r.json()
        assert any(h["process_name"] == "bot" and h["tick_count"] == 99
                   for h in payload["heartbeats"])
        assert len(payload["recent_actions"]) >= 1
    finally:
        server_module.app.dependency_overrides.clear()
