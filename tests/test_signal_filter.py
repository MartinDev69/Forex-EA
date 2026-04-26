"""SignalFilter — pass-through, thresholding, save/load roundtrip, Bot integration."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("xgboost")

from src.bot import Bot, BotConfig
from src.execution.journal import TradeJournal
from src.execution.mock import MockExecutor
from src.ml.features import FEATURE_NAMES
from src.ml.signal_filter import SignalFilter
from src.risk.risk_manager import RiskLimits, RiskManager
from src.strategies.base import Signal, SignalType, Strategy


def _ohlc(bars: int = 60, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 1.1 + np.cumsum(rng.normal(0, 0.0005, bars))
    high = close + 0.0008
    low = close - 0.0008
    idx = pd.date_range("2024-01-01", periods=bars, freq="15min")
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": 100.0},
        index=idx,
    )


def _signal(ohlc: pd.DataFrame) -> Signal:
    last = float(ohlc["close"].iloc[-1])
    return Signal(
        type=SignalType.BUY, symbol="EURUSD",
        timestamp=ohlc.index[-1].to_pydatetime(),
        price=last, stop_loss=last - 0.003, take_profit=last + 0.006,
    )


def _train_toy_model():
    """Tiny XGBoost model trained on random features — shape only, not predictive."""
    from xgboost import XGBClassifier

    rng = np.random.default_rng(0)
    X = rng.normal(size=(60, len(FEATURE_NAMES)))
    # Simple decidable pattern: class 1 when first feature > 0.
    y = (X[:, 0] > 0).astype(int)
    model = XGBClassifier(n_estimators=20, max_depth=3, random_state=0, eval_metric="logloss")
    model.fit(X, y)
    return model


def test_no_model_is_passthrough():
    f = SignalFilter(model=None)
    ohlc = _ohlc()
    assert f.should_take(_signal(ohlc), ohlc, "ma_crossover") is True
    assert f.predict_proba(_signal(ohlc), ohlc, "ma_crossover") == 0.5


def test_threshold_gates_predictions():
    model = _train_toy_model()
    f = SignalFilter(model=model, threshold=0.99)  # basically never takes
    ohlc = _ohlc()
    # With a threshold this high, most signals should be rejected.
    # We construct several random inputs and ensure at least one is rejected.
    any_skipped = False
    for seed in range(10):
        oh = _ohlc(seed=seed)
        if not f.should_take(_signal(oh), oh, "ma_crossover"):
            any_skipped = True
            break
    assert any_skipped, "threshold=0.99 should reject at least one sample"


def test_save_load_roundtrip(tmp_path: Path):
    model = _train_toy_model()
    f = SignalFilter(model=model, threshold=0.5)
    ohlc = _ohlc()
    p_before = f.predict_proba(_signal(ohlc), ohlc, "ma_crossover")

    path = tmp_path / "m.json"
    f.save(path)
    loaded = SignalFilter.load(path, threshold=0.5)
    p_after = loaded.predict_proba(_signal(ohlc), ohlc, "ma_crossover")

    assert abs(p_before - p_after) < 1e-6


class AlwaysBuyStrategy(Strategy):
    name = "always_buy"

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        return _signal(ohlc)


class _FixedFeed:
    def __init__(self, ohlc): self._ohlc = ohlc
    def latest_bars(self, symbol, timeframe, count): return self._ohlc.tail(count).copy()


def test_bot_skips_when_filter_rejects(tmp_path: Path):
    """End-to-end: filter threshold=1.01 → nothing passes → bot never places orders."""
    ohlc = _ohlc()
    feed = _FixedFeed(ohlc)
    executor = MockExecutor(starting_balance=10_000)
    risk = RiskManager(RiskLimits(risk_per_trade=0.01, max_open_trades=5))
    journal = TradeJournal(tmp_path / "trades.db")

    filt = SignalFilter(model=_train_toy_model(), threshold=1.01)

    bot = Bot(
        config=BotConfig(symbols=["EURUSD"], timeframe="M15"),
        strategies={"EURUSD": [AlwaysBuyStrategy("EURUSD")]},
        data_feed=feed, executor=executor, risk_manager=risk,
        journal=journal, signal_filter=filt,
    )

    assert bot.tick() == 0
    assert executor.open_orders() == []


def test_bot_allows_when_filter_accepts(tmp_path: Path):
    ohlc = _ohlc()
    feed = _FixedFeed(ohlc)
    executor = MockExecutor(starting_balance=10_000)
    risk = RiskManager(RiskLimits(risk_per_trade=0.01, max_open_trades=5))
    journal = TradeJournal(tmp_path / "trades.db")

    # threshold=0.0 → always take
    filt = SignalFilter(model=_train_toy_model(), threshold=0.0)

    bot = Bot(
        config=BotConfig(symbols=["EURUSD"], timeframe="M15"),
        strategies={"EURUSD": [AlwaysBuyStrategy("EURUSD")]},
        data_feed=feed, executor=executor, risk_manager=risk,
        journal=journal, signal_filter=filt,
    )

    assert bot.tick() == 1
    assert len(executor.open_orders()) == 1
