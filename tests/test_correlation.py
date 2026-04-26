"""Tests for correlation calculator, store, throttle, RiskManager wiring, and API."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.correlation.calculator import CorrelationCalculator, CorrelationConfig
from src.correlation.store import CorrelationStore
from src.correlation.throttle import (
    OpenPosition,
    PortfolioThrottle,
    ThrottlePolicy,
)
from src.risk.position_sizing import lot_size_from_risk
from src.risk.risk_manager import RiskLimits, RiskManager


# --------------------------------------------------------------- helpers

def _series(values: np.ndarray, start="2024-01-01", freq="15min") -> pd.Series:
    idx = pd.date_range(start, periods=len(values), freq=freq)
    return pd.Series(values, index=idx)


def _correlated_pair(n: int, rho: float, seed: int = 1) -> tuple[pd.Series, pd.Series]:
    rng = np.random.default_rng(seed)
    a = rng.normal(0, 0.001, n)
    noise = rng.normal(0, 0.001, n)
    b = rho * a + np.sqrt(1 - rho * rho) * noise
    pa = 1.10 + np.cumsum(a)
    pb = 1.30 + np.cumsum(b)
    return _series(pa), _series(pb)


# ------------------------------------------------------------ calculator

def test_calculator_correlated_series_yield_high_correlation():
    a, b = _correlated_pair(300, rho=0.9)
    matrix = CorrelationCalculator(CorrelationConfig(window_bars=300, min_observations=50)).matrix(
        {"A": a, "B": b}
    )
    assert matrix.loc["A", "B"] > 0.7


def test_calculator_independent_series_yield_low_correlation():
    a, _ = _correlated_pair(300, rho=0.0, seed=1)
    _, b = _correlated_pair(300, rho=0.0, seed=99)
    matrix = CorrelationCalculator(CorrelationConfig(window_bars=300, min_observations=50)).matrix(
        {"A": a, "B": b}
    )
    assert abs(matrix.loc["A", "B"]) < 0.4


def test_calculator_empty_when_too_few_bars():
    a, b = _correlated_pair(40, rho=0.9)
    matrix = CorrelationCalculator(CorrelationConfig(min_observations=100)).matrix({"A": a, "B": b})
    assert matrix.empty


def test_calculator_drops_misaligned_symbol():
    # B has only 30 bars overlapping after the inner-join; we want it dropped.
    a, b_full = _correlated_pair(300, rho=0.9)
    b = b_full.iloc[-30:]
    matrix = CorrelationCalculator(CorrelationConfig(min_observations=50)).matrix({"A": a, "B": b})
    # Inner join with only 30 overlapping rows fails the min_observations check
    # and the matrix collapses entirely.
    assert matrix.empty


def test_config_from_env():
    cfg = CorrelationConfig.from_env({
        "CORRELATION_WINDOW_BARS": "500",
        "CORRELATION_MIN_OBS": "120",
    })
    assert cfg.window_bars == 500
    assert cfg.min_observations == 120


# ----------------------------------------------------------------- store

def test_store_roundtrip_pair_and_matrix(tmp_path: Path):
    store = CorrelationStore(tmp_path / "trades.db")
    df = pd.DataFrame(
        [[1.0, 0.85, -0.10],
         [0.85, 1.0, 0.05],
         [-0.10, 0.05, 1.0]],
        index=["EURUSD", "GBPUSD", "USDJPY"],
        columns=["EURUSD", "GBPUSD", "USDJPY"],
    )
    wrote = store.upsert_matrix(df, window_bars=200)
    # 3 unique unordered pairs.
    assert wrote == 3

    assert store.pair("EURUSD", "GBPUSD") == pytest.approx(0.85)
    # Lookup is order-independent.
    assert store.pair("GBPUSD", "EURUSD") == pytest.approx(0.85)
    assert store.pair("EURUSD", "EURUSD") == 1.0
    assert store.pair("EURUSD", "AUDUSD") is None


def test_store_upsert_replaces_value(tmp_path: Path):
    store = CorrelationStore(tmp_path / "trades.db")
    df1 = pd.DataFrame([[1.0, 0.20], [0.20, 1.0]], index=["A", "B"], columns=["A", "B"])
    df2 = pd.DataFrame([[1.0, 0.95], [0.95, 1.0]], index=["A", "B"], columns=["A", "B"])
    store.upsert_matrix(df1, 100)
    store.upsert_matrix(df2, 100)
    assert store.pair("A", "B") == pytest.approx(0.95)


def test_store_all_pairs_sorted_by_abs(tmp_path: Path):
    store = CorrelationStore(tmp_path / "trades.db")
    df = pd.DataFrame(
        [[1.0, 0.10, -0.95],
         [0.10, 1.0, 0.20],
         [-0.95, 0.20, 1.0]],
        index=["A", "B", "C"], columns=["A", "B", "C"],
    )
    store.upsert_matrix(df, 100)
    pairs = store.all_pairs()
    assert [(p["symbol_a"], p["symbol_b"]) for p in pairs][0] == ("A", "C")  # |−0.95| wins


# -------------------------------------------------------------- throttle

@pytest.fixture
def store_with_corrs(tmp_path: Path) -> CorrelationStore:
    store = CorrelationStore(tmp_path / "trades.db")
    df = pd.DataFrame(
        [[1.0, 0.85, -0.10, 0.20],
         [0.85, 1.0, -0.20, 0.15],
         [-0.10, -0.20, 1.0, 0.05],
         [0.20, 0.15, 0.05, 1.0]],
        index=["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
        columns=["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
    )
    store.upsert_matrix(df, 200)
    return store


def test_throttle_disabled_always_approves(store_with_corrs):
    t = PortfolioThrottle(store_with_corrs, ThrottlePolicy(enabled=False))
    d = t.decide("EURUSD", "BUY", 0.01, [OpenPosition("GBPUSD", "BUY", 0.01)])
    assert d.approved is True


def test_throttle_no_open_positions_approves(store_with_corrs):
    t = PortfolioThrottle(store_with_corrs)
    d = t.decide("EURUSD", "BUY", 0.01, [])
    assert d.approved is True


def test_throttle_rejects_correlated_pile_on(store_with_corrs):
    # 3 BUYs already open on highly correlated names + candidate BUY EURUSD.
    # Effective heat: 3 × 0.01 × 0.85 = 0.0255; + 0.01 candidate = 0.0355. With
    # max_correlated_heat_pct=0.03, this should be rejected.
    t = PortfolioThrottle(
        store_with_corrs,
        ThrottlePolicy(max_correlated_heat_pct=0.03, correlation_floor=0.30),
    )
    open_pos = [
        OpenPosition("GBPUSD", "BUY", 0.01),
        OpenPosition("GBPUSD", "BUY", 0.01),
        OpenPosition("GBPUSD", "BUY", 0.01),
    ]
    d = t.decide("EURUSD", "BUY", 0.01, open_pos)
    assert d.approved is False
    assert "correlated heat" in d.reason


def test_throttle_allows_hedge(store_with_corrs):
    # BUY GBPUSD open; SELL EURUSD candidate. Same-direction corr is 0.85,
    # so opposite-side effective is -0.85 → contribution clipped to 0.
    t = PortfolioThrottle(store_with_corrs)
    d = t.decide("EURUSD", "SELL", 0.01, [OpenPosition("GBPUSD", "BUY", 0.01)])
    assert d.approved is True
    assert d.effective_heat == 0.0


def test_throttle_ignores_below_floor(store_with_corrs):
    # USDJPY ↔ EURUSD = -0.10, below floor 0.30 → contribution zero.
    t = PortfolioThrottle(store_with_corrs, ThrottlePolicy(correlation_floor=0.30))
    d = t.decide("EURUSD", "BUY", 0.01, [OpenPosition("USDJPY", "SELL", 0.01)])
    assert d.approved is True
    assert d.effective_heat == 0.0


def test_throttle_unknown_pair_is_conservative(tmp_path: Path):
    store = CorrelationStore(tmp_path / "trades.db")  # nothing written
    t = PortfolioThrottle(
        store,
        ThrottlePolicy(unknown_pair_correlation=1.0, max_correlated_heat_pct=0.015),
    )
    d = t.decide("EURUSD", "BUY", 0.01, [OpenPosition("USDCAD", "BUY", 0.01)])
    # Treated as fully correlated → 0.01 + 0.01 = 0.02 > 0.015 → reject
    assert d.approved is False


def test_throttle_same_symbol_pile_on(store_with_corrs):
    t = PortfolioThrottle(store_with_corrs, ThrottlePolicy(max_correlated_heat_pct=0.015))
    d = t.decide(
        "EURUSD", "BUY", 0.01,
        [OpenPosition("EURUSD", "BUY", 0.01)],
    )
    assert d.approved is False  # 0.01 + 0.01 = 0.02 > 0.015


# ----------------------------------------------------- RiskManager wiring

def test_risk_manager_calls_throttle(store_with_corrs):
    rm = RiskManager(
        RiskLimits(risk_per_trade=0.01, max_open_trades=10, max_portfolio_heat_pct=1.0),
        portfolio_throttle=PortfolioThrottle(
            store_with_corrs,
            ThrottlePolicy(max_correlated_heat_pct=0.015),
        ),
    )
    open_pos = [OpenPosition("EURUSD", "BUY", 0.01)]
    decision = rm.evaluate(
        account_balance=10_000,
        stop_distance_pips=20,
        symbol="GBPUSD",  # 0.85 correlated with EURUSD
        lot_sizer=lot_size_from_risk,
        side="BUY",
        open_positions=open_pos,
    )
    assert decision.approved is False
    assert "correlated heat" in decision.reason


def test_risk_manager_skips_throttle_without_side(store_with_corrs):
    rm = RiskManager(
        RiskLimits(),
        portfolio_throttle=PortfolioThrottle(store_with_corrs),
    )
    decision = rm.evaluate(
        account_balance=10_000, stop_distance_pips=20,
        symbol="GBPUSD", lot_sizer=lambda **_: 0.1,
        # `side` and `open_positions` not provided → throttle skipped
    )
    assert decision.approved is True


# --------------------------------------------------------------- API

@pytest.fixture
def correlation_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.api import auth as auth_module
    from src.api import server as server_module

    store = CorrelationStore(tmp_path / "trades.db")
    monkeypatch.setattr(server_module, "correlation_store", store)
    server_module.app.dependency_overrides[auth_module.current_user] = lambda: {
        "username": "test", "role": "admin",
    }
    client = TestClient(server_module.app)
    yield client, store
    server_module.app.dependency_overrides.clear()


def test_correlation_endpoint_empty(correlation_api):
    client, _ = correlation_api
    r = client.get("/correlation")
    assert r.status_code == 200
    assert r.json() == {"pairs": [], "count": 0}


def test_correlation_endpoint_returns_pairs(correlation_api):
    client, store = correlation_api
    df = pd.DataFrame(
        [[1.0, 0.85], [0.85, 1.0]],
        index=["EURUSD", "GBPUSD"], columns=["EURUSD", "GBPUSD"],
    )
    store.upsert_matrix(df, 200)
    r = client.get("/correlation")
    body = r.json()
    assert body["count"] == 1
    assert body["pairs"][0]["symbol_a"] == "EURUSD"
    assert body["pairs"][0]["symbol_b"] == "GBPUSD"
    assert body["pairs"][0]["value"] == pytest.approx(0.85)
