"""Tests for the champion-challenger allocator: score, allocate, store, API."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.allocator.allocator import (
    Allocation,
    AllocatorPolicy,
    ChampionChallengerAllocator,
)
from src.allocator.score import StrategyScore, score_pairs
from src.allocator.store import AllocationStore


# ---------------------------------------------------------------- helpers


def _seed_trade(
    db_path: Path,
    *,
    trade_id: int,
    strategy: str,
    symbol: str,
    side: str = "BUY",
    entry: float = 1.1000,
    stop: float = 1.0950,
    exit_price: float = 1.1100,
    pnl: float = 100.0,
    closed_at: datetime | None = None,
) -> None:
    schema = """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, lot_size REAL,
        entry_price REAL, exit_price REAL, stop_loss REAL, take_profit REAL,
        strategy TEXT, status TEXT, opened_at TEXT, closed_at TEXT,
        pnl REAL DEFAULT 0, close_reason TEXT, broker_ticket INTEGER
    );
    """
    closed = closed_at or datetime.now(timezone.utc)
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
                strategy, closed.isoformat(), closed.isoformat(), pnl,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _make_score(strategy: str, symbol: str, *, avg_r: float, samples: int = 30,
                win_rate: float = 0.55) -> StrategyScore:
    return StrategyScore(
        strategy=strategy, symbol=symbol, sample_size=samples,
        avg_r=avg_r, win_rate=win_rate,
        computed_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------- score


def test_score_empty_pair_returns_zero_sample(tmp_path):
    db = tmp_path / "trades.db"
    # Seed an unrelated trade so the table exists.
    _seed_trade(db, trade_id=1, strategy="other", symbol="USDJPY")
    [score] = score_pairs(db, [("ma_crossover", "EURUSD")])
    assert score.sample_size == 0
    assert score.avg_r == 0.0


def test_score_winners_only(tmp_path):
    db = tmp_path / "trades.db"
    # 5 winners, exit at 2R.
    for i in range(5):
        _seed_trade(
            db, trade_id=i, strategy="ma_crossover", symbol="EURUSD",
            entry=1.1000, stop=1.0950, exit_price=1.1100, pnl=200.0,
        )
    [score] = score_pairs(db, [("ma_crossover", "EURUSD")])
    assert score.sample_size == 5
    assert score.win_rate == 1.0
    assert score.avg_r == pytest.approx(2.0, rel=0.01)


def test_score_mixed(tmp_path):
    db = tmp_path / "trades.db"
    # 3 winners @ +2R, 2 losers @ -1R → avg_r = (3*2 - 2*1) / 5 = 0.8
    for i in range(3):
        _seed_trade(db, trade_id=i, strategy="s1", symbol="EURUSD",
                    entry=1.1, stop=1.095, exit_price=1.11, pnl=100.0)
    for i in range(3, 5):
        _seed_trade(db, trade_id=i, strategy="s1", symbol="EURUSD",
                    entry=1.1, stop=1.095, exit_price=1.095, pnl=-50.0)
    [score] = score_pairs(db, [("s1", "EURUSD")])
    assert score.sample_size == 5
    assert score.win_rate == pytest.approx(0.6)
    assert score.avg_r == pytest.approx(0.8, rel=0.01)


def test_score_sell_side_signs_correctly(tmp_path):
    db = tmp_path / "trades.db"
    # SELL: entry 1.1, stop 1.105, exit 1.09 → favorable, +2R after sign flip.
    _seed_trade(db, trade_id=1, strategy="s1", symbol="EURUSD",
                side="SELL", entry=1.10, stop=1.105, exit_price=1.09, pnl=100.0)
    [score] = score_pairs(db, [("s1", "EURUSD")])
    assert score.avg_r == pytest.approx(2.0, rel=0.01)


def test_score_window_caps_samples(tmp_path):
    db = tmp_path / "trades.db"
    base = datetime.now(timezone.utc)
    for i in range(50):
        _seed_trade(
            db, trade_id=i, strategy="s1", symbol="EURUSD",
            closed_at=base.replace(microsecond=i),
        )
    [score] = score_pairs(db, [("s1", "EURUSD")], window=10)
    assert score.sample_size == 10


# ---------------------------------------------------------------- allocate


def test_single_eligible_strategy_is_champion():
    a = ChampionChallengerAllocator(AllocatorPolicy(min_samples=5))
    [alloc] = a.allocate([_make_score("s1", "EURUSD", avg_r=0.5, samples=20)])
    assert alloc.role == "champion"
    assert alloc.weight == 1.0


def test_challenger_within_tolerance_gets_mid_weight():
    a = ChampionChallengerAllocator(
        AllocatorPolicy(min_samples=5, challenger_tolerance=0.20)
    )
    out = a.allocate([
        _make_score("s1", "EURUSD", avg_r=0.50),  # champion
        _make_score("s2", "EURUSD", avg_r=0.40),  # within 0.20 → challenger
        _make_score("s3", "EURUSD", avg_r=0.10),  # gap 0.40 → probe
    ])
    by_strat = {a.strategy: a for a in out}
    assert by_strat["s1"].role == "champion"
    assert by_strat["s2"].role == "challenger"
    assert by_strat["s3"].role == "probe"


def test_below_min_samples_is_cold():
    a = ChampionChallengerAllocator(AllocatorPolicy(min_samples=20))
    [alloc] = a.allocate([_make_score("s1", "EURUSD", avg_r=2.0, samples=5)])
    assert alloc.role == "cold"
    assert alloc.weight == 0.0


def test_floor_kills_full_weight_when_all_underwater():
    a = ChampionChallengerAllocator(
        AllocatorPolicy(min_samples=5, floor_avg_r=-0.10)
    )
    out = a.allocate([
        _make_score("s1", "EURUSD", avg_r=-0.30),
        _make_score("s2", "EURUSD", avg_r=-0.50),
    ])
    # Champion would be s1 at -0.30, but that's below the floor — both probe.
    for alloc in out:
        assert alloc.role == "probe"
        assert alloc.weight == pytest.approx(0.1)


def test_per_symbol_grouping():
    """Champions of different symbols don't compete with each other."""
    a = ChampionChallengerAllocator(AllocatorPolicy(min_samples=5))
    out = a.allocate([
        _make_score("s1", "EURUSD", avg_r=0.5),
        _make_score("s1", "USDJPY", avg_r=0.1),  # only one on USDJPY → still champion
    ])
    by_pair = {(x.strategy, x.symbol): x for x in out}
    assert by_pair[("s1", "EURUSD")].role == "champion"
    assert by_pair[("s1", "USDJPY")].role == "champion"


# ---------------------------------------------------------------- store


def _alloc(strategy="s1", symbol="EURUSD", role="champion", weight=1.0) -> Allocation:
    return Allocation(
        strategy=strategy, symbol=symbol, role=role, weight=weight,
        sample_size=20, avg_r=0.5, win_rate=0.6, note="test",
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


def test_store_upsert_and_read(tmp_path):
    store = AllocationStore(tmp_path / "trades.db")
    store.upsert_many([_alloc(), _alloc(strategy="s2", role="probe", weight=0.1)])
    rows = store.all()
    assert len(rows) == 2
    assert {r.strategy for r in rows} == {"s1", "s2"}


def test_store_upsert_replaces(tmp_path):
    store = AllocationStore(tmp_path / "trades.db")
    store.upsert_many([_alloc(weight=1.0)])
    store.upsert_many([_alloc(weight=0.5, role="challenger")])
    rows = store.all()
    assert len(rows) == 1
    assert rows[0].weight == 0.5
    assert rows[0].role == "challenger"


def test_store_get_missing_returns_none(tmp_path):
    store = AllocationStore(tmp_path / "trades.db")
    assert store.get("nope", "EURUSD") is None


# ---------------------------------------------------------------- API


@pytest.fixture
def allocator_api(tmp_path):
    from fastapi.testclient import TestClient
    from src.api import auth as auth_module
    from src.api import server as server_module

    db = tmp_path / "trades.db"
    store = AllocationStore(db)
    server_module.allocation_store = store
    server_module._allocator_cache["value"] = None
    server_module._allocator_cache["expires_at"] = 0.0
    server_module.app.dependency_overrides[auth_module.current_user] = lambda: {
        "username": "test", "role": "admin",
    }
    client = TestClient(server_module.app)
    yield client, store
    server_module.app.dependency_overrides.clear()


def test_allocator_endpoint_empty(allocator_api):
    client, _ = allocator_api
    r = client.get("/allocator")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["allocations"] == []


def test_allocator_endpoint_returns_rows(allocator_api):
    client, store = allocator_api
    store.upsert_many([_alloc(), _alloc(strategy="s2", role="probe", weight=0.1)])
    r = client.get("/allocator")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    roles = {a["strategy"]: a["role"] for a in body["allocations"]}
    assert roles == {"s1": "champion", "s2": "probe"}
