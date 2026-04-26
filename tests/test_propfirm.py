"""Tests for the prop-firm challenge guard: policy parsing, store roundtrip,
observe/check/note_trade_opened semantics, kill-flag transitions, RiskManager
integration, and the /propfirm API surface.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.propfirm import (
    PRESETS,
    PropFirmGuard,
    PropFirmPolicy,
    PropFirmStore,
    policy_from_env,
)
from src.risk.position_sizing import lot_size_from_risk
from src.risk.risk_manager import RiskLimits, RiskManager


def _now() -> datetime:
    return datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)


def _ftmo_policy(initial: float = 10_000.0) -> PropFirmPolicy:
    return PropFirmPolicy(
        initial_balance=initial,
        max_daily_loss_pct=0.05,
        max_total_drawdown_pct=0.10,
        profit_target_pct=0.10,
        min_trading_days=4,
        max_lot_size=None,
        require_stop_loss=True,
        drawdown_from_peak=False,
        preset_name="ftmo",
    )


# ---------------------------------------------------------------- policy

def test_policy_amounts_derived_from_initial_balance():
    p = _ftmo_policy(20_000.0)
    assert p.daily_loss_amount == pytest.approx(1_000.0)
    assert p.max_drawdown_amount == pytest.approx(2_000.0)
    assert p.profit_target_amount == pytest.approx(2_000.0)


def test_policy_from_env_uses_preset_defaults():
    env = {"PROPFIRM_PRESET": "ftmo", "PROPFIRM_INITIAL_BALANCE": "50000"}
    p = policy_from_env(env)
    preset = PRESETS["ftmo"]
    assert p.preset_name == "ftmo"
    assert p.initial_balance == 50_000.0
    assert p.max_daily_loss_pct == preset["max_daily_loss_pct"]
    assert p.drawdown_from_peak is preset["drawdown_from_peak"]


def test_policy_from_env_override_beats_preset():
    env = {
        "PROPFIRM_PRESET": "ftmo",
        "PROPFIRM_INITIAL_BALANCE": "10000",
        "PROPFIRM_MAX_DAILY_LOSS_PCT": "0.02",
        "PROPFIRM_REQUIRE_STOP_LOSS": "0",
    }
    p = policy_from_env(env)
    assert p.max_daily_loss_pct == 0.02
    assert p.require_stop_loss is False


def test_policy_from_env_custom_skips_preset():
    env = {
        "PROPFIRM_PRESET": "custom",
        "PROPFIRM_INITIAL_BALANCE": "25000",
        "PROPFIRM_MAX_DAILY_LOSS_PCT": "0.03",
        "PROPFIRM_MAX_TOTAL_DD_PCT": "0.08",
        "PROPFIRM_PROFIT_TARGET_PCT": "0.07",
        "PROPFIRM_DD_FROM_PEAK": "1",
    }
    p = policy_from_env(env)
    assert p.preset_name == "custom"
    assert p.initial_balance == 25_000.0
    assert p.drawdown_from_peak is True


# ---------------------------------------------------------------- store

def test_store_initialize_is_idempotent(tmp_path):
    store = PropFirmStore(tmp_path / "trades.db")
    today = date(2026, 4, 26)
    s1 = store.initialize(10_000.0, today, _now())
    s2 = store.initialize(99_999.0, today, _now())
    assert s1.initial_balance == 10_000.0
    assert s2.initial_balance == 10_000.0  # second call is a no-op


def test_store_write_roundtrip(tmp_path):
    store = PropFirmStore(tmp_path / "trades.db")
    today = date(2026, 4, 26)
    state = store.initialize(10_000.0, today, _now())
    state.peak_equity = 10_500.0
    state.killed_today = True
    state.killed_reason = "test"
    store.write(state)
    got = store.read()
    assert got.peak_equity == 10_500.0
    assert got.killed_today is True
    assert got.killed_reason == "test"


# ---------------------------------------------------------------- observe

def _make_guard(tmp_path, *, policy=None, clock=None):
    store = PropFirmStore(tmp_path / "trades.db")
    pol = policy or _ftmo_policy()
    return PropFirmGuard(pol, store, clock=clock or _now), store


def test_observe_initializes_state_on_first_call(tmp_path):
    guard, store = _make_guard(tmp_path)
    guard.observe(account_balance=10_000.0)
    state = store.read()
    assert state is not None
    assert state.initial_balance == 10_000.0
    assert state.daily_start_equity == 10_000.0
    assert state.peak_equity == 10_000.0


def test_observe_tracks_peak(tmp_path):
    guard, store = _make_guard(tmp_path)
    guard.observe(10_000.0)
    guard.observe(10_500.0)
    guard.observe(10_200.0)  # drawdown shouldn't lower peak
    assert store.read().peak_equity == 10_500.0


def test_observe_flips_killed_today_on_daily_dd_breach(tmp_path):
    guard, store = _make_guard(tmp_path)
    guard.observe(10_000.0)
    guard.observe(9_500.0)  # exactly 5%
    state = store.read()
    assert state.killed_today is True
    assert "daily DD" in state.killed_reason


def test_observe_under_daily_threshold_does_not_kill(tmp_path):
    guard, store = _make_guard(tmp_path)
    guard.observe(10_000.0)
    guard.observe(9_600.0)  # 4% — under 5% threshold
    assert store.read().killed_today is False


def test_observe_flips_killed_permanently_on_total_dd(tmp_path):
    guard, store = _make_guard(tmp_path)
    guard.observe(10_000.0)
    guard.observe(9_000.0)  # exactly 10% from initial
    state = store.read()
    assert state.killed_permanently is True
    assert "total DD" in state.killed_reason


def test_observe_total_dd_from_peak_when_configured(tmp_path):
    pol = PropFirmPolicy(
        initial_balance=10_000.0,
        max_daily_loss_pct=0.05,
        max_total_drawdown_pct=0.10,
        profit_target_pct=0.10,
        min_trading_days=0,
        max_lot_size=None,
        require_stop_loss=False,
        drawdown_from_peak=True,
        preset_name="custom",
    )
    guard, store = _make_guard(tmp_path, policy=pol)
    guard.observe(10_000.0)
    guard.observe(12_000.0)  # peak rises to 12k
    # 10% from peak = 10_800 — anything below is breach.
    guard.observe(10_700.0)
    state = store.read()
    assert state.killed_permanently is True


def test_observe_rolls_daily_window_at_midnight(tmp_path):
    clock_state = {"now": _now()}
    clock = lambda: clock_state["now"]
    guard, store = _make_guard(tmp_path, clock=clock)
    guard.observe(10_000.0)
    # First, breach daily DD.
    guard.observe(9_400.0)
    assert store.read().killed_today is True
    # New day rolls over.
    clock_state["now"] = _now() + timedelta(days=1)
    guard.observe(9_400.0)
    state = store.read()
    assert state.killed_today is False
    assert state.daily_start_equity == 9_400.0
    assert state.daily_start_date == clock_state["now"].date()


# ---------------------------------------------------------------- check

def test_check_rejects_when_killed_today(tmp_path):
    guard, store = _make_guard(tmp_path)
    guard.observe(10_000.0)
    guard.observe(9_400.0)
    decision = guard.check(account_balance=9_400.0, signal_has_stop=True, candidate_lot=0.5)
    assert decision.approved is False
    assert "daily" in decision.reason


def test_check_rejects_when_killed_permanently(tmp_path):
    guard, store = _make_guard(tmp_path)
    guard.observe(10_000.0)
    guard.observe(9_000.0)
    decision = guard.check(account_balance=9_000.0, signal_has_stop=True, candidate_lot=0.5)
    assert decision.approved is False
    assert "killed" in decision.reason


def test_check_requires_stop_when_policy_demands(tmp_path):
    guard, store = _make_guard(tmp_path)  # ftmo policy w/ require_stop_loss=True
    guard.observe(10_000.0)
    decision = guard.check(account_balance=10_000.0, signal_has_stop=False, candidate_lot=0.5)
    assert decision.approved is False
    assert "stop loss" in decision.reason


def test_check_enforces_lot_cap(tmp_path):
    pol = PropFirmPolicy(
        initial_balance=10_000.0,
        max_daily_loss_pct=0.05,
        max_total_drawdown_pct=0.10,
        profit_target_pct=0.10,
        min_trading_days=0,
        max_lot_size=1.0,
        require_stop_loss=False,
        drawdown_from_peak=False,
        preset_name="custom",
    )
    guard, store = _make_guard(tmp_path, policy=pol)
    guard.observe(10_000.0)
    decision = guard.check(account_balance=10_000.0, signal_has_stop=True, candidate_lot=2.0)
    assert decision.approved is False
    assert "cap" in decision.reason


def test_check_approves_clean_request(tmp_path):
    guard, store = _make_guard(tmp_path)
    guard.observe(10_000.0)
    decision = guard.check(account_balance=10_000.0, signal_has_stop=True, candidate_lot=0.5)
    assert decision.approved is True


# ---------------------------------------------------------------- bookkeeping

def test_note_trade_opened_bumps_count_once_per_day(tmp_path):
    clock_state = {"now": _now()}
    clock = lambda: clock_state["now"]
    guard, store = _make_guard(tmp_path, clock=clock)
    guard.observe(10_000.0)
    guard.note_trade_opened()
    guard.note_trade_opened()  # same day — no second bump
    assert store.read().trading_days_count == 1
    clock_state["now"] = _now() + timedelta(days=1)
    guard.note_trade_opened()
    assert store.read().trading_days_count == 2


# ---------------------------------------------------------------- progress

def test_progress_snapshot_has_all_fields(tmp_path):
    guard, store = _make_guard(tmp_path)
    guard.observe(10_000.0)
    snap = guard.progress(account_balance=10_300.0)
    assert snap["initialized"] is True
    assert snap["preset"] == "ftmo"
    assert snap["profit_amount"] == pytest.approx(300.0)
    assert snap["profit_target_amount"] == pytest.approx(1_000.0)
    assert snap["daily_loss_amount"] == 0.0
    assert snap["killed_today"] is False
    assert snap["killed_permanently"] is False


def test_progress_uninitialized_returns_minimal(tmp_path):
    store = PropFirmStore(tmp_path / "trades.db")
    guard = PropFirmGuard(_ftmo_policy(), store)
    snap = guard.progress(10_000.0)
    assert snap["initialized"] is False
    assert snap["preset"] == "ftmo"


# ---------------------------------------------------------------- RiskManager integration

def test_risk_manager_rejects_when_propfirm_killed(tmp_path):
    guard, store = _make_guard(tmp_path)
    guard.observe(10_000.0)
    guard.observe(9_400.0)  # kill daily
    rm = RiskManager(RiskLimits(), propfirm_guard=guard)
    decision = rm.evaluate(
        account_balance=9_400.0,
        stop_distance_pips=50,
        symbol="EURUSD",
        lot_sizer=lot_size_from_risk,
    )
    assert decision.approved is False
    assert "propfirm" in decision.reason


def test_risk_manager_rejects_when_lot_exceeds_propfirm_cap(tmp_path):
    pol = PropFirmPolicy(
        initial_balance=10_000.0,
        max_daily_loss_pct=0.05,
        max_total_drawdown_pct=0.10,
        profit_target_pct=0.10,
        min_trading_days=0,
        max_lot_size=0.05,  # tiny cap so the computed 0.20 lots gets rejected
        require_stop_loss=False,
        drawdown_from_peak=False,
        preset_name="custom",
    )
    guard, store = _make_guard(tmp_path, policy=pol)
    guard.observe(10_000.0)
    rm = RiskManager(RiskLimits(), propfirm_guard=guard)
    decision = rm.evaluate(
        account_balance=10_000.0,
        stop_distance_pips=50,
        symbol="EURUSD",
        lot_sizer=lot_size_from_risk,
    )
    assert decision.approved is False
    assert "propfirm" in decision.reason


def test_risk_manager_approves_when_propfirm_clean(tmp_path):
    guard, store = _make_guard(tmp_path)
    guard.observe(10_000.0)
    rm = RiskManager(RiskLimits(), propfirm_guard=guard)
    decision = rm.evaluate(
        account_balance=10_000.0,
        stop_distance_pips=50,
        symbol="EURUSD",
        lot_sizer=lot_size_from_risk,
    )
    assert decision.approved is True
    assert decision.lot_size > 0


# ---------------------------------------------------------------- API

def test_propfirm_endpoint_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "x" * 64)
    monkeypatch.delenv("PROPFIRM_ENABLED", raising=False)
    from src.api import auth as auth_module
    from src.api import server as server_module
    server_module.app.dependency_overrides[auth_module.current_user] = lambda: {
        "username": "t", "role": "admin",
    }
    try:
        client = TestClient(server_module.app)
        r = client.get("/propfirm")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["initialized"] is False
    finally:
        server_module.app.dependency_overrides.clear()


def test_propfirm_endpoint_returns_progress_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "x" * 64)
    monkeypatch.setenv("PROPFIRM_ENABLED", "1")
    monkeypatch.setenv("PROPFIRM_PRESET", "ftmo")
    monkeypatch.setenv("PROPFIRM_INITIAL_BALANCE", "10000")
    db = tmp_path / "trades.db"
    store = PropFirmStore(db)
    PropFirmGuard(_ftmo_policy(), store).observe(10_000.0)

    from src.api import auth as auth_module
    from src.api import server as server_module
    monkeypatch.setattr(server_module, "_DB", db)
    monkeypatch.setattr(server_module, "propfirm_store", store)
    server_module.app.dependency_overrides[auth_module.current_user] = lambda: {
        "username": "t", "role": "admin",
    }
    try:
        client = TestClient(server_module.app)
        r = client.get("/propfirm")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["initialized"] is True
        assert body["preset"] == "ftmo"
        assert body["initial_balance"] == 10_000.0
    finally:
        server_module.app.dependency_overrides.clear()
