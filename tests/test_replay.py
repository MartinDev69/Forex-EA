"""Tests for replay-with-different-params: path store, recorder slicing,
engine SL/TP walk semantics, edge cases, and the /trades/{id}/replay
endpoint.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.replay import (
    PathBar,
    PathRecorder,
    PathStore,
    ReplayEngine,
    ReplayRequest,
)


def _ts(minutes: int = 0) -> datetime:
    return datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


# ---------------------------------------------------------------- path store


def test_path_store_roundtrip(tmp_path):
    s = PathStore(tmp_path / "trades.db")
    bars = [
        PathBar(ts="2026-04-26T10:00:00+00:00", open=1.10, high=1.11, low=1.09, close=1.105),
        PathBar(ts="2026-04-26T10:15:00+00:00", open=1.105, high=1.12, low=1.10, close=1.115),
    ]
    s.write(42, bars)
    got = s.read(42)
    assert len(got) == 2
    assert got[0].open == 1.10
    assert got[1].close == 1.115


def test_path_store_overwrites(tmp_path):
    s = PathStore(tmp_path / "trades.db")
    s.write(1, [PathBar(ts="t1", open=1.0, high=1.0, low=1.0, close=1.0)])
    s.write(1, [
        PathBar(ts="u1", open=2.0, high=2.0, low=2.0, close=2.0),
        PathBar(ts="u2", open=3.0, high=3.0, low=3.0, close=3.0),
    ])
    got = s.read(1)
    assert [b.open for b in got] == [2.0, 3.0]


def test_path_store_empty_for_unknown(tmp_path):
    s = PathStore(tmp_path / "trades.db")
    assert s.read(999) == []


def test_path_store_skips_empty_write(tmp_path):
    s = PathStore(tmp_path / "trades.db")
    s.write(7, [])
    assert s.read(7) == []


# ---------------------------------------------------------------- recorder


class _FakeFeed:
    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.calls = 0

    def latest_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        self.calls += 1
        return self.df


def _make_df(n: int) -> pd.DataFrame:
    rows = []
    for i in range(n):
        rows.append({
            "time": _ts(i * 15).isoformat(),
            "open": 1.10 + i * 0.001,
            "high": 1.11 + i * 0.001,
            "low": 1.09 + i * 0.001,
            "close": 1.105 + i * 0.001,
        })
    return pd.DataFrame(rows)


def test_recorder_slices_bars_in_window(tmp_path):
    df = _make_df(10)
    feed = _FakeFeed(df)
    store = PathStore(tmp_path / "trades.db")
    rec = PathRecorder(feed, store, timeframe="M15")
    n = rec.record(
        trade_id=1, symbol="EURUSD",
        opened_at=_ts(15),     # bar 1
        closed_at=_ts(15 * 4), # bar 4
    )
    # 4 bars: indices 1..4 inclusive
    assert n == 4
    assert len(store.read(1)) == 4


def test_recorder_handles_empty_feed(tmp_path):
    feed = _FakeFeed(pd.DataFrame())
    store = PathStore(tmp_path / "trades.db")
    rec = PathRecorder(feed, store, timeframe="M15")
    assert rec.record(1, "EURUSD", _ts(0), _ts(60)) == 0


def test_recorder_swallows_feed_exception(tmp_path):
    class _Boom:
        def latest_bars(self, *a, **k):
            raise RuntimeError("network down")
    rec = PathRecorder(_Boom(), PathStore(tmp_path / "trades.db"), timeframe="M15")
    assert rec.record(1, "EURUSD", _ts(0), _ts(60)) == 0


def test_recorder_falls_back_to_tail_when_no_overlap(tmp_path):
    # Window is way before any bar in the df → no overlap. The recorder
    # should still record the tail so the engine has something to work with.
    df = _make_df(60)
    feed = _FakeFeed(df)
    store = PathStore(tmp_path / "trades.db")
    rec = PathRecorder(feed, store, timeframe="M15")
    n = rec.record(
        trade_id=1, symbol="EURUSD",
        opened_at=_ts(-1000),
        closed_at=_ts(-900),
    )
    assert n > 0


# ---------------------------------------------------------------- engine: walk semantics


def _seed_closed_trade(
    db: Path,
    *,
    side: str = "BUY",
    entry: float = 1.10000,
    sl: float = 1.09800,
    tp: float = 1.10500,
    pnl: float = 100.0,
    close_reason: str = "target",
) -> int:
    from src.execution.journal import TradeJournal
    TradeJournal(db)
    with sqlite3.connect(db) as c:
        c.execute(
            """INSERT INTO trades (id, symbol, side, lot_size, entry_price,
                exit_price, stop_loss, take_profit, strategy, status,
                opened_at, closed_at, pnl, close_reason)
               VALUES (1, 'EURUSD', ?, 0.20, ?, ?, ?, ?, 'X', 'CLOSED',
                       '2026-04-26T10:00:00+00:00',
                       '2026-04-26T11:00:00+00:00', ?, ?)""",
            (side, entry, tp if close_reason == "target" else sl,
             sl, tp, pnl, close_reason),
        )
    return 1


def test_engine_target_hit_replay(tmp_path):
    db = tmp_path / "trades.db"
    _seed_closed_trade(db)
    store = PathStore(db)
    # Bar where high reaches target.
    store.write(1, [
        PathBar(ts="t1", open=1.10000, high=1.10100, low=1.09900, close=1.10050),
        PathBar(ts="t2", open=1.10050, high=1.10600, low=1.10000, close=1.10500),
    ])
    eng = ReplayEngine(store, db_path=db)
    res = eng.replay(1, ReplayRequest())  # original levels
    assert res.replay_close_reason == "target"
    assert res.replay_exit_price == pytest.approx(1.10500)
    assert res.bars_walked == 2
    assert res.replay_pnl > 0
    assert res.pnl_delta == pytest.approx(0.0, abs=0.01)


def test_engine_stop_first_when_both_in_same_bar(tmp_path):
    db = tmp_path / "trades.db"
    _seed_closed_trade(db)
    store = PathStore(db)
    # First bar's range covers both SL and TP — engine takes SL conservatively.
    store.write(1, [
        PathBar(ts="t1", open=1.10000, high=1.10600, low=1.09700, close=1.10000),
    ])
    eng = ReplayEngine(store, db_path=db)
    res = eng.replay(1, ReplayRequest())
    assert res.replay_close_reason == "stop"
    assert res.replay_exit_price == pytest.approx(1.09800)


def test_engine_open_at_end_when_no_touch(tmp_path):
    db = tmp_path / "trades.db"
    _seed_closed_trade(db)
    store = PathStore(db)
    # Range never touches SL or TP.
    store.write(1, [
        PathBar(ts="t1", open=1.10000, high=1.10100, low=1.09900, close=1.10050),
        PathBar(ts="t2", open=1.10050, high=1.10200, low=1.09950, close=1.10100),
    ])
    eng = ReplayEngine(store, db_path=db)
    res = eng.replay(1, ReplayRequest())
    assert res.replay_close_reason == "open_at_end"
    assert res.replay_exit_price == pytest.approx(1.10100)


def test_engine_widening_stop_avoids_premature_exit(tmp_path):
    db = tmp_path / "trades.db"
    _seed_closed_trade(db, pnl=-200.0, close_reason="stop")
    store = PathStore(db)
    # Original SL=1.098 gets hit on bar 1, but a wider SL=1.097 does not.
    # Then bar 2 reaches the target at 1.105.
    store.write(1, [
        PathBar(ts="t1", open=1.10000, high=1.10000, low=1.09780, close=1.09850),
        PathBar(ts="t2", open=1.09850, high=1.10550, low=1.09850, close=1.10500),
    ])
    eng = ReplayEngine(store, db_path=db)
    res = eng.replay(1, ReplayRequest(stop_loss=1.09700))
    assert res.replay_close_reason == "target"
    assert res.replay_pnl > 0
    assert res.pnl_delta > 0  # the wider stop saves money


def test_engine_sl_multiplier(tmp_path):
    db = tmp_path / "trades.db"
    _seed_closed_trade(db)
    store = PathStore(db)
    store.write(1, [PathBar(ts="t1", open=1.10000, high=1.10100, low=1.09900, close=1.10050)])
    eng = ReplayEngine(store, db_path=db)
    # Original SL distance = 1.10000 - 1.09800 = 0.002. Mult 2.0 → SL at 1.096.
    res = eng.replay(1, ReplayRequest(sl_mult=2.0))
    assert res.replay_stop == pytest.approx(1.09600)


def test_engine_tp_multiplier_for_sell(tmp_path):
    db = tmp_path / "trades.db"
    # Sell entry 1.10, TP 1.095, SL 1.103
    _seed_closed_trade(db, side="SELL", entry=1.10000, sl=1.10300, tp=1.09500)
    store = PathStore(db)
    store.write(1, [PathBar(ts="t1", open=1.10000, high=1.10100, low=1.09900, close=1.10000)])
    eng = ReplayEngine(store, db_path=db)
    # TP distance = 0.005, mult 2 → TP at 1.090 (further down).
    res = eng.replay(1, ReplayRequest(tp_mult=2.0))
    assert res.replay_target == pytest.approx(1.09000)


def test_engine_returns_no_path_when_unrecorded(tmp_path):
    db = tmp_path / "trades.db"
    _seed_closed_trade(db)
    eng = ReplayEngine(PathStore(db), db_path=db)
    res = eng.replay(1, ReplayRequest())
    assert res is not None
    assert res.replay_close_reason == "no_path"
    assert res.bars_walked == 0


def test_engine_returns_none_for_unknown_trade(tmp_path):
    db = tmp_path / "trades.db"
    from src.execution.journal import TradeJournal
    TradeJournal(db)  # ensure schema
    eng = ReplayEngine(PathStore(db), db_path=db)
    assert eng.replay(999, ReplayRequest()) is None


def test_engine_returns_none_for_open_trade(tmp_path):
    db = tmp_path / "trades.db"
    from src.execution.journal import TradeJournal
    TradeJournal(db)
    with sqlite3.connect(db) as c:
        c.execute(
            """INSERT INTO trades (id, symbol, side, lot_size, entry_price,
                stop_loss, take_profit, strategy, status, opened_at, pnl)
               VALUES (1, 'EURUSD', 'BUY', 0.20, 1.10000, 1.09800, 1.10500,
                       'X', 'OPEN', '2026-04-26T10:00:00+00:00', 0)"""
        )
    eng = ReplayEngine(PathStore(db), db_path=db)
    assert eng.replay(1, ReplayRequest()) is None


def test_engine_sell_target_hit(tmp_path):
    db = tmp_path / "trades.db"
    _seed_closed_trade(db, side="SELL", entry=1.10000, sl=1.10300, tp=1.09500)
    store = PathStore(db)
    store.write(1, [PathBar(ts="t1", open=1.10000, high=1.10100, low=1.09400, close=1.09500)])
    eng = ReplayEngine(store, db_path=db)
    res = eng.replay(1, ReplayRequest())
    assert res.replay_close_reason == "target"
    assert res.replay_pnl > 0


def test_engine_r_multiple_correct(tmp_path):
    db = tmp_path / "trades.db"
    _seed_closed_trade(db)
    store = PathStore(db)
    store.write(1, [PathBar(ts="t1", open=1.10000, high=1.10500, low=1.09850, close=1.10500)])
    eng = ReplayEngine(store, db_path=db)
    res = eng.replay(1, ReplayRequest())
    # Risk = 0.002, reward = 0.005 → 2.5R
    assert res.replay_r_multiple == pytest.approx(2.5)


# ---------------------------------------------------------------- API


def test_replay_endpoint_404_for_unknown_trade(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "x" * 64)
    db = tmp_path / "trades.db"
    from src.execution.journal import TradeJournal
    TradeJournal(db)  # ensure 'trades' table exists
    PathStore(db)
    from src.api import auth as auth_module
    from src.api import server as server_module
    monkeypatch.setattr(server_module, "_DB", db)
    monkeypatch.setattr(server_module, "path_store", PathStore(db))
    server_module.app.dependency_overrides[auth_module.current_user] = lambda: {
        "username": "t", "role": "admin",
    }
    try:
        client = TestClient(server_module.app)
        r = client.post("/trades/999/replay", json={})
        assert r.status_code == 404
    finally:
        server_module.app.dependency_overrides.clear()


def test_replay_endpoint_returns_alternative_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "x" * 64)
    db = tmp_path / "trades.db"
    _seed_closed_trade(db)
    store = PathStore(db)
    store.write(1, [
        PathBar(ts="t1", open=1.10000, high=1.10550, low=1.09850, close=1.10500),
    ])
    from src.api import auth as auth_module
    from src.api import server as server_module
    monkeypatch.setattr(server_module, "_DB", db)
    monkeypatch.setattr(server_module, "path_store", store)
    server_module.app.dependency_overrides[auth_module.current_user] = lambda: {
        "username": "t", "role": "admin",
    }
    try:
        client = TestClient(server_module.app)
        r = client.post("/trades/1/replay", json={"sl_mult": 2.0})
        assert r.status_code == 200
        body = r.json()
        assert body["trade_id"] == 1
        assert body["replay_stop"] == pytest.approx(1.09600)
        assert body["replay_close_reason"] == "target"
    finally:
        server_module.app.dependency_overrides.clear()
