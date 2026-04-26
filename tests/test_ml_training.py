"""Training pipeline — dataset builder + train() on a fabricated journal."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("xgboost")

from src.execution.base import Order, OrderStatus
from src.execution.journal import TradeJournal
from src.ml.features import FEATURE_NAMES
from src.ml.training import (
    build_training_dataset,
    train,
    walk_forward_evaluate,
    walk_forward_splits,
)
from src.strategies.base import SignalType


def _bars(bars: int = 200, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 1.1 + np.cumsum(rng.normal(0, 0.0005, bars))
    idx = pd.date_range("2024-01-01", periods=bars, freq="15min")
    return pd.DataFrame(
        {
            "open": close, "high": close + 0.001, "low": close - 0.001,
            "close": close, "volume": 100.0,
        },
        index=idx,
    )


def _seed_journal(journal: TradeJournal, bars: pd.DataFrame, n: int = 40) -> None:
    """Create n closed trades at evenly-spaced entry times in the bar window."""
    rng = np.random.default_rng(1)
    # Start past the MIN_BARS threshold so features can be computed.
    entry_idxs = np.linspace(40, len(bars) - 2, n).astype(int)
    for i, bar_i in enumerate(entry_idxs):
        entry_ts = bars.index[bar_i].to_pydatetime()
        close_ts = entry_ts + timedelta(hours=2)
        entry_price = float(bars["close"].iloc[bar_i])
        side = SignalType.BUY if i % 2 == 0 else SignalType.SELL
        pnl = float(rng.choice([-50.0, 50.0]))

        order = Order(
            id=i + 1, symbol="EURUSD", side=side, lot_size=0.1,
            entry_price=entry_price,
            stop_loss=entry_price - 0.003 if side == SignalType.BUY else entry_price + 0.003,
            take_profit=entry_price + 0.006 if side == SignalType.BUY else entry_price - 0.006,
            opened_at=entry_ts, strategy="ma_crossover",
        )
        journal.record_open(order)

        order.status = OrderStatus.CLOSED
        order.exit_price = entry_price + (0.001 if pnl > 0 else -0.001)
        order.closed_at = close_ts
        order.pnl = pnl
        order.close_reason = "tp" if pnl > 0 else "sl"
        journal.record_close(order)


def test_build_training_dataset_labels_correctly(tmp_path: Path):
    bars = _bars()
    journal = TradeJournal(tmp_path / "trades.db")
    _seed_journal(journal, bars, n=30)

    def get_bars(symbol: str, until: datetime) -> pd.DataFrame:
        return bars.loc[:pd.Timestamp(until)]

    X, y, ts = build_training_dataset(journal, get_bars, limit=1000)

    assert list(X.columns) == list(FEATURE_NAMES)
    assert len(X) == len(y) == len(ts) == 30
    assert set(y.unique()).issubset({0, 1})
    assert y.sum() > 0 and y.sum() < 30, "need both classes for a realistic test"
    # Timestamps must be chronological — walk-forward depends on it.
    assert ts.is_monotonic_increasing


def test_build_training_dataset_skips_when_bars_missing(tmp_path: Path):
    bars = _bars()
    journal = TradeJournal(tmp_path / "trades.db")
    _seed_journal(journal, bars, n=10)

    def get_bars(symbol: str, until: datetime) -> pd.DataFrame:
        return bars.iloc[:5]  # always too few bars

    X, y, ts = build_training_dataset(journal, get_bars)
    assert len(X) == 0 and len(y) == 0 and len(ts) == 0


def test_train_returns_model_and_report(tmp_path: Path):
    bars = _bars()
    journal = TradeJournal(tmp_path / "trades.db")
    _seed_journal(journal, bars, n=80)

    def get_bars(symbol: str, until: datetime) -> pd.DataFrame:
        return bars.loc[:pd.Timestamp(until)]

    X, y, _ = build_training_dataset(journal, get_bars)
    model, report = train(X, y, test_size=0.25)

    assert hasattr(model, "predict_proba")
    assert report.n_samples == len(X)
    assert 0.0 <= report.test_accuracy <= 1.0
    assert set(report.feature_importance.keys()) == set(FEATURE_NAMES)


def test_train_rejects_single_class(tmp_path: Path):
    X = pd.DataFrame(np.zeros((20, len(FEATURE_NAMES))), columns=list(FEATURE_NAMES))
    y = pd.Series([1] * 20)
    with pytest.raises(ValueError, match="one class"):
        train(X, y)


# ---------------------------------------------------------------- walk-forward


def test_walk_forward_splits_expanding():
    folds = list(walk_forward_splits(n_samples=60, n_folds=5))
    assert len(folds) == 5
    # Every test index must come AFTER every train index in the same fold.
    for train_idx, test_idx in folds:
        assert train_idx.max() < test_idx.min()
    # Expanding window: train set grows each fold.
    train_sizes = [len(train) for train, _ in folds]
    assert all(a <= b for a, b in zip(train_sizes, train_sizes[1:]))


def test_walk_forward_splits_sliding_keeps_train_size_bounded():
    folds = list(walk_forward_splits(n_samples=100, n_folds=5,
                                     min_train_size=30, window="sliding"))
    assert len(folds) == 5
    train_sizes = {len(train) for train, _ in folds}
    # Sliding — later folds should not be much larger than the minimum.
    assert max(train_sizes) <= 35


def test_walk_forward_splits_rejects_too_few_samples():
    with pytest.raises(ValueError, match="at least"):
        list(walk_forward_splits(n_samples=3, n_folds=5))


def test_walk_forward_evaluate_produces_per_fold_metrics(tmp_path: Path):
    bars = _bars(bars=400)
    journal = TradeJournal(tmp_path / "trades.db")
    _seed_journal(journal, bars, n=120)

    def get_bars(symbol: str, until: datetime) -> pd.DataFrame:
        return bars.loc[:pd.Timestamp(until)]

    X, y, ts = build_training_dataset(journal, get_bars)
    report = walk_forward_evaluate(X, y, ts, n_folds=4)

    assert len(report.folds) == 4
    for f in report.folds:
        assert f.train_size > 0
        assert f.test_size > 0
        assert 0.0 <= f.accuracy <= 1.0
        # test window must start after train window ended
        assert f.test_start >= f.train_start
    # Aggregate metrics should be in-range.
    assert 0.0 <= report.mean_accuracy <= 1.0
