"""Tests for drift baseline store, monitor logic, and API endpoint."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.drift.baseline import Baseline, BaselineStore
from src.drift.monitor import DriftConfig, DriftMonitor


# ---------------------------------------------------------------- helpers


def _seed_trade(
    db_path: Path,
    *,
    trade_id: int,
    strategy: str,
    symbol: str,
    side: str,
    entry: float,
    stop: float,
    exit_price: float,
    pnl: float,
    closed_at: datetime,
) -> None:
    """Insert a CLOSED trade row into the journal table the monitor reads."""
    # Mirror the journal schema so we don't need to import its private bits.
    schema = """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, lot_size REAL,
        entry_price REAL, exit_price REAL, stop_loss REAL, take_profit REAL,
        strategy TEXT, status TEXT, opened_at TEXT, closed_at TEXT,
        pnl REAL DEFAULT 0, close_reason TEXT, broker_ticket INTEGER
    );
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema)
        conn.execute(
            """
            INSERT INTO trades (id, symbol, side, lot_size, entry_price,
                exit_price, stop_loss, take_profit, strategy, status,
                opened_at, closed_at, pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?, ?, ?)
            """,
            (
                trade_id, symbol, side, 0.1, entry, exit_price, stop,
                entry + 2 * (entry - stop) * (1 if side == "BUY" else -1),
                strategy, closed_at.isoformat(), closed_at.isoformat(), pnl,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _baseline(strategy="ma_crossover", symbol="EURUSD", **overrides) -> Baseline:
    defaults = dict(
        strategy=strategy,
        symbol=symbol,
        trade_count=200,
        win_rate=0.55,
        avg_r=0.40,
        avg_trades_per_day=2.0,
        source="backtest",
        computed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Baseline(**defaults)


# ---------------------------------------------------------------- store


def test_baseline_store_roundtrip(tmp_path):
    store = BaselineStore(tmp_path / "trades.db")
    b = _baseline()
    store.upsert(b)
    got = store.get("ma_crossover", "EURUSD")
    assert got is not None
    assert got.win_rate == pytest.approx(0.55)
    assert got.trade_count == 200


def test_baseline_store_upsert_replaces(tmp_path):
    store = BaselineStore(tmp_path / "trades.db")
    store.upsert(_baseline(win_rate=0.40))
    store.upsert(_baseline(win_rate=0.60))
    got = store.get("ma_crossover", "EURUSD")
    assert got.win_rate == pytest.approx(0.60)
    assert len(store.all()) == 1


def test_baseline_store_delete(tmp_path):
    store = BaselineStore(tmp_path / "trades.db")
    store.upsert(_baseline())
    store.delete("ma_crossover", "EURUSD")
    assert store.get("ma_crossover", "EURUSD") is None


# ---------------------------------------------------------------- monitor


def _seed_winning_streak(db_path: Path, n: int, strategy="ma_crossover", symbol="EURUSD") -> None:
    base_time = datetime(2024, 6, 1, tzinfo=timezone.utc)
    for i in range(n):
        # Half wins (target=+2R), half losses (stop=-1R) so win_rate=0.5, avg_r=0.5
        is_win = i % 2 == 0
        _seed_trade(
            db_path,
            trade_id=i + 1,
            strategy=strategy,
            symbol=symbol,
            side="BUY",
            entry=1.1000,
            stop=1.0980,        # 20-pip stop
            exit_price=1.1040 if is_win else 1.0980,  # +40 or -20 pips
            pnl=10.0 if is_win else -5.0,
            closed_at=base_time + timedelta(hours=i),
        )


def test_monitor_returns_unknown_below_min_trades(tmp_path):
    db = tmp_path / "trades.db"
    store = BaselineStore(db)
    store.upsert(_baseline())
    _seed_winning_streak(db, n=3)  # below default min_live_trades=10
    monitor = DriftMonitor(db, store, DriftConfig(min_live_trades=10))
    reports = monitor.report()
    assert len(reports) == 1
    assert reports[0].status == "unknown"
    assert reports[0].live_trade_count == 3


def test_monitor_ok_when_live_matches_baseline(tmp_path):
    db = tmp_path / "trades.db"
    store = BaselineStore(db)
    # Baseline win_rate=0.50, avg_r=0.50 — matches the synthetic streak.
    store.upsert(_baseline(win_rate=0.50, avg_r=0.50, avg_trades_per_day=2.0))
    _seed_winning_streak(db, n=20)
    monitor = DriftMonitor(db, store, DriftConfig(min_live_trades=10, warn_delta=0.10))
    reports = monitor.report()
    assert reports[0].status == "ok"
    # Win rate should be ~0.50, avg_r ~0.50.
    by_name = {m.name: m for m in reports[0].metrics}
    assert abs(by_name["win_rate"].live - 0.50) < 0.01
    assert abs(by_name["avg_r"].live - 0.50) < 0.01


def test_monitor_danger_when_winrate_collapses(tmp_path):
    db = tmp_path / "trades.db"
    store = BaselineStore(db)
    store.upsert(_baseline(win_rate=0.55, avg_r=0.40))
    # Force all losses → win_rate=0, avg_r=-1
    base_time = datetime(2024, 6, 1, tzinfo=timezone.utc)
    for i in range(15):
        _seed_trade(
            db, trade_id=i + 1, strategy="ma_crossover", symbol="EURUSD",
            side="BUY", entry=1.1000, stop=1.0980,
            exit_price=1.0980, pnl=-5.0,
            closed_at=base_time + timedelta(hours=i),
        )
    monitor = DriftMonitor(db, store, DriftConfig(
        min_live_trades=10, warn_delta=0.10, danger_delta=0.20,
    ))
    reports = monitor.report()
    assert reports[0].status == "danger"


def test_monitor_only_reports_pairs_with_baselines(tmp_path):
    db = tmp_path / "trades.db"
    store = BaselineStore(db)
    store.upsert(_baseline(strategy="ma_crossover", symbol="EURUSD"))
    # Seed trades for a different strategy — should not appear.
    _seed_winning_streak(db, n=15, strategy="rsi_mean_reversion")
    monitor = DriftMonitor(db, store, DriftConfig(min_live_trades=10))
    reports = monitor.report()
    assert len(reports) == 1
    assert reports[0].strategy == "ma_crossover"


def test_monitor_handles_zero_baseline_division(tmp_path):
    db = tmp_path / "trades.db"
    store = BaselineStore(db)
    store.upsert(_baseline(win_rate=0.0, avg_r=0.0))
    _seed_winning_streak(db, n=15)
    monitor = DriftMonitor(db, store, DriftConfig(min_live_trades=10))
    reports = monitor.report()
    # delta_pct should not blow up when baseline is zero.
    for m in reports[0].metrics:
        assert m.delta_pct == 0.0 or isinstance(m.delta_pct, float)


def test_monitor_serialization(tmp_path):
    db = tmp_path / "trades.db"
    store = BaselineStore(db)
    store.upsert(_baseline())
    _seed_winning_streak(db, n=15)
    monitor = DriftMonitor(db, store, DriftConfig(min_live_trades=10))
    payload = monitor.report()[0].to_dict()
    assert payload["strategy"] == "ma_crossover"
    assert "metrics" in payload
    assert payload["baseline"]["source"] == "backtest"


# ---------------------------------------------------------------- API


@pytest.fixture
def drift_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.api import auth as auth_module
    from src.api import server as server_module

    db = tmp_path / "trades.db"
    bstore = BaselineStore(db)
    monkeypatch.setattr(server_module, "drift_baseline_store", bstore)
    monkeypatch.setattr(
        server_module, "drift_monitor",
        DriftMonitor(db, bstore, DriftConfig(min_live_trades=10)),
    )
    # Reset the in-memory cache between tests.
    server_module._drift_cache["value"] = None
    server_module._drift_cache["expires_at"] = 0.0
    server_module.app.dependency_overrides[auth_module.current_user] = lambda: {
        "username": "test", "role": "admin",
    }
    client = TestClient(server_module.app)
    yield client, bstore, db
    server_module.app.dependency_overrides.clear()


def test_drift_endpoint_empty(drift_api):
    client, _, _ = drift_api
    r = client.get("/drift")
    assert r.status_code == 200
    body = r.json()
    assert body == {"reports": [], "count": 0, "cached_at": body["cached_at"]}


def test_drift_endpoint_with_baseline_and_trades(drift_api):
    client, bstore, db = drift_api
    bstore.upsert(_baseline(win_rate=0.50, avg_r=0.50))
    _seed_winning_streak(db, n=20)
    r = client.get("/drift")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["reports"][0]["status"] == "ok"
