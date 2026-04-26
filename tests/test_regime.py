"""Tests for market-regime classifier + Bot gating + API endpoint."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.bot import Bot, BotConfig
from src.execution.journal import TradeJournal
from src.execution.mock import MockExecutor
from src.indicators.directional import adx as adx_indicator
from src.regime import (
    RegimeClassifier,
    RegimeConfig,
    RegimeStore,
    TrendRegime,
    VolatilityRegime,
    empty_snapshot_dict,
)
from src.risk.risk_manager import RiskLimits, RiskManager
from src.strategies.base import Signal, SignalType, Strategy


# ---------------------------------------------------------- synthetic bars

def _bars(closes: np.ndarray, spread: float = 0.0005, start="2024-01-01", freq="15min") -> pd.DataFrame:
    idx = pd.date_range(start, periods=len(closes), freq=freq)
    high = closes + spread
    low = closes - spread
    return pd.DataFrame(
        {"open": closes, "high": high, "low": low, "close": closes, "volume": 100},
        index=idx,
    )


def _trending_up(n: int = 300, slope: float = 0.0008) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    noise = rng.normal(0, 0.0001, n)
    closes = 1.1000 + slope * np.arange(n) + noise
    return _bars(closes)


def _ranging(n: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    closes = 1.1000 + rng.normal(0, 0.0003, n)
    return _bars(closes)


def _trending_down(n: int = 300, slope: float = 0.0008) -> pd.DataFrame:
    rng = np.random.default_rng(9)
    noise = rng.normal(0, 0.0001, n)
    closes = 1.2000 - slope * np.arange(n) + noise
    return _bars(closes)


# ------------------------------------------------------------- indicator

def test_adx_returns_three_columns_and_values_in_range():
    ohlc = _trending_up()
    di = adx_indicator(ohlc["high"], ohlc["low"], ohlc["close"], period=14)
    assert set(di.columns) == {"adx", "plus_di", "minus_di"}
    # Drop the warm-up NaNs; remaining values must be in [0, 100].
    for col in di.columns:
        vals = di[col].dropna()
        assert (vals >= 0).all()
        assert (vals <= 100).all()


def test_adx_trend_higher_than_range():
    trend = _trending_up()
    rng = _ranging()
    trend_adx = adx_indicator(trend["high"], trend["low"], trend["close"])["adx"].dropna().iloc[-1]
    range_adx = adx_indicator(rng["high"], rng["low"], rng["close"])["adx"].dropna().iloc[-1]
    assert trend_adx > range_adx


# ------------------------------------------------------------ classifier

def test_classifier_detects_trend_up():
    snap = RegimeClassifier().classify(_trending_up())
    assert snap.trend == TrendRegime.TREND_UP
    assert snap.adx is not None and snap.adx >= 22


def test_classifier_detects_trend_down():
    snap = RegimeClassifier().classify(_trending_down())
    assert snap.trend == TrendRegime.TREND_DOWN


def test_classifier_detects_range():
    snap = RegimeClassifier().classify(_ranging())
    assert snap.trend == TrendRegime.RANGE


def test_classifier_unknown_on_short_input():
    snap = RegimeClassifier().classify(_bars(np.full(10, 1.1)))
    assert snap.trend == TrendRegime.UNKNOWN
    assert snap.volatility == VolatilityRegime.UNKNOWN
    assert snap.label == "unknown"


def test_classifier_volatility_rank():
    # A bar with elevated high/low range sitting at the tail of a calmer window
    # should be flagged as high volatility.
    rng = np.random.default_rng(3)
    base = 1.1 + np.cumsum(rng.normal(0, 0.0001, 300))
    # Inject a volatility spike in the last 20 bars.
    spike = rng.normal(0, 0.002, 20)
    base[-20:] = base[-20] + np.cumsum(spike)
    df = pd.DataFrame(
        {"open": base, "high": base + 0.002, "low": base - 0.002, "close": base, "volume": 100},
        index=pd.date_range("2024-01-01", periods=300, freq="15min"),
    )
    snap = RegimeClassifier().classify(df)
    assert snap.volatility == VolatilityRegime.HIGH


def test_classifier_label_format():
    snap = RegimeClassifier().classify(_trending_up())
    assert ":" in snap.label
    trend, vol = snap.label.split(":")
    assert trend == snap.trend.value
    assert vol == snap.volatility.value


def test_regime_config_from_env_reads_numeric_knobs():
    cfg = RegimeConfig.from_env({
        "REGIME_ADX_PERIOD": "21",
        "REGIME_ADX_TREND_THRESHOLD": "30",
        "REGIME_ATR_PERIOD": "10",
        "REGIME_ATR_LOOKBACK": "50",
        "REGIME_ATR_LOW_PCT": "0.25",
        "REGIME_ATR_HIGH_PCT": "0.75",
    })
    assert cfg.adx_period == 21
    assert cfg.adx_trend_threshold == 30
    assert cfg.atr_period == 10
    assert cfg.atr_lookback == 50


# ------------------------------------------------------------------ store

def test_store_upsert_and_get_roundtrip(tmp_path: Path):
    store = RegimeStore(tmp_path / "trades.db")
    snap = RegimeClassifier().classify(_trending_up())
    store.upsert("EURUSD", snap)

    got = store.get("EURUSD")
    assert got is not None
    assert got["trend"] == snap.trend.value
    assert got["volatility"] == snap.volatility.value
    assert got["stored_at"] is not None


def test_store_upsert_replaces_previous(tmp_path: Path):
    store = RegimeStore(tmp_path / "trades.db")
    up = RegimeClassifier().classify(_trending_up())
    down = RegimeClassifier().classify(_trending_down())
    store.upsert("EURUSD", up)
    store.upsert("EURUSD", down)
    got = store.get("EURUSD")
    assert got["trend"] == TrendRegime.TREND_DOWN.value


def test_empty_snapshot_shape():
    d = empty_snapshot_dict("USDJPY")
    assert d["symbol"] == "USDJPY"
    assert d["trend"] == "unknown"
    assert d["label"] == "unknown"


# ------------------------------------------------------------ bot gating

class _FixedFeed:
    def __init__(self, ohlc: pd.DataFrame) -> None:
        self._ohlc = ohlc

    def latest_bars(self, symbol, timeframe, count):
        return self._ohlc.tail(count).copy()


class _RangeOnlyStrategy(Strategy):
    name = "range_only"
    preferred_regimes = frozenset({"range"})

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        price = float(ohlc["close"].iloc[-1])
        return Signal(
            type=SignalType.BUY, symbol=self.symbol, timestamp=ohlc.index[-1],
            price=price, stop_loss=price - 0.005, take_profit=price + 0.010,
        )


class _TrendOnlyStrategy(Strategy):
    name = "trend_only"
    preferred_regimes = frozenset({"trend_up", "trend_down"})

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        price = float(ohlc["close"].iloc[-1])
        return Signal(
            type=SignalType.BUY, symbol=self.symbol, timestamp=ohlc.index[-1],
            price=price, stop_loss=price - 0.005, take_profit=price + 0.010,
        )


def _bot(tmp_path, strategy, feed):
    executor = MockExecutor(starting_balance=10_000)
    return Bot(
        config=BotConfig(symbols=["EURUSD"], timeframe="M15", poll_interval_s=1),
        strategies={"EURUSD": [strategy]},
        data_feed=feed,
        executor=executor,
        risk_manager=RiskManager(RiskLimits(risk_per_trade=0.01, max_open_trades=5)),
        journal=TradeJournal(tmp_path / "trades.db"),
        regime_classifier=RegimeClassifier(),
        regime_store=RegimeStore(tmp_path / "trades.db"),
    )


def test_bot_skips_range_strategy_in_trend(tmp_path):
    bot = _bot(tmp_path, _RangeOnlyStrategy("EURUSD"), _FixedFeed(_trending_up()))
    assert bot.tick() == 0
    assert bot.state.last_regime["EURUSD"].trend == TrendRegime.TREND_UP


def test_bot_runs_range_strategy_in_range(tmp_path):
    bot = _bot(tmp_path, _RangeOnlyStrategy("EURUSD"), _FixedFeed(_ranging()))
    assert bot.tick() == 1


def test_bot_runs_trend_strategy_in_trend(tmp_path):
    bot = _bot(tmp_path, _TrendOnlyStrategy("EURUSD"), _FixedFeed(_trending_up()))
    assert bot.tick() == 1


def test_bot_persists_regime_to_store(tmp_path):
    bot = _bot(tmp_path, _RangeOnlyStrategy("EURUSD"), _FixedFeed(_ranging()))
    bot.tick()
    stored = bot.regime_store.get("EURUSD")
    assert stored is not None
    assert stored["trend"] == TrendRegime.RANGE.value


def test_bot_without_classifier_is_permissive(tmp_path):
    # preferred_regimes should only matter when a classifier is plugged in.
    feed = _FixedFeed(_trending_up())
    bot = Bot(
        config=BotConfig(symbols=["EURUSD"], timeframe="M15", poll_interval_s=1),
        strategies={"EURUSD": [_RangeOnlyStrategy("EURUSD")]},
        data_feed=feed,
        executor=MockExecutor(starting_balance=10_000),
        risk_manager=RiskManager(RiskLimits(risk_per_trade=0.01, max_open_trades=5)),
        journal=TradeJournal(tmp_path / "trades.db"),
    )
    assert bot.tick() == 1


def test_bot_unknown_regime_is_permissive(tmp_path):
    # Too-few bars → UNKNOWN regime → gate lets everything through.
    short = _bars(np.full(15, 1.1))
    feed = _FixedFeed(short)
    bot = _bot(tmp_path, _RangeOnlyStrategy("EURUSD"), feed)
    # Trade doesn't fire because the strategy check happens, but the reason is
    # that "insufficient bars" triggers downstream; here the strategy fires on
    # any bar, so the gate being permissive means it reaches the signal step
    # and acts.
    assert bot.tick() == 1


# --------------------------------------------------------------- API

@pytest.fixture
def regime_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.api import auth as auth_module
    from src.api import server as server_module

    store = RegimeStore(tmp_path / "trades.db")
    monkeypatch.setattr(server_module, "regime_store", store)

    stub = lambda: {"username": "test-user", "role": "admin"}
    server_module.app.dependency_overrides[auth_module.current_user] = stub

    client = TestClient(server_module.app)
    yield client, store
    server_module.app.dependency_overrides.clear()


def test_regime_endpoint_returns_unknown_when_empty(regime_api):
    client, _ = regime_api
    r = client.get("/regime/EURUSD")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "EURUSD"
    assert body["trend"] == "unknown"


def test_regime_endpoint_returns_stored_snapshot(regime_api):
    client, store = regime_api
    snap = RegimeClassifier().classify(_trending_up())
    store.upsert("EURUSD", snap)
    r = client.get("/regime/EURUSD")
    assert r.status_code == 200
    body = r.json()
    assert body["trend"] == TrendRegime.TREND_UP.value
    assert body["adx"] is not None
