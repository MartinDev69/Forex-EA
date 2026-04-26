"""Training pipeline for the signal meta-model.

The journal gives us labels (closed trades with pnl). For each row we need the
OHLC window the strategy saw at entry — that's what the live filter will see
too, so we replay it from a caller-supplied `get_bars` function.

Caller owns the bar source (parquet/CSV/MT5 history). This module just orchestrates.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterator

import numpy as np
import pandas as pd

from src.execution.journal import TradeJournal
from src.strategies.base import Signal, SignalType

from .features import FEATURE_NAMES, MIN_BARS, build_feature_vector

log = logging.getLogger(__name__)

BarLookup = Callable[[str, datetime], pd.DataFrame]


@dataclass
class TrainingReport:
    n_samples: int
    n_wins: int
    n_losses: int
    test_accuracy: float
    test_auc: float
    feature_importance: dict[str, float]


@dataclass
class FoldResult:
    fold: int
    train_size: int
    test_size: int
    train_start: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    accuracy: float
    auc: float


@dataclass
class WalkForwardReport:
    """Out-of-sample metrics from time-series cross-validation.

    `mean_accuracy` / `mean_auc` are the headline numbers — they reflect how
    the model is likely to perform on fresh data, without the optimistic bias
    that a random train/test split would produce for a time-ordered series.
    """
    folds: list[FoldResult] = field(default_factory=list)

    @property
    def mean_accuracy(self) -> float:
        return float(np.mean([f.accuracy for f in self.folds])) if self.folds else float("nan")

    @property
    def mean_auc(self) -> float:
        values = [f.auc for f in self.folds if not np.isnan(f.auc)]
        return float(np.mean(values)) if values else float("nan")

    @property
    def std_auc(self) -> float:
        values = [f.auc for f in self.folds if not np.isnan(f.auc)]
        return float(np.std(values)) if len(values) > 1 else 0.0


def build_training_dataset(
    journal: TradeJournal,
    get_bars: BarLookup,
    limit: int = 10_000,
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Read closed trades, recreate each entry's feature vector, label by pnl sign.

    Returns (X, y, timestamps) sorted chronologically by entry time.
    `timestamps` drives walk-forward CV — without it we'd be forced into a
    random split that leaks future information into training.
    """
    rows = journal.recent(limit=limit)
    feature_rows: list[pd.Series] = []
    labels: list[int] = []
    entry_times: list[pd.Timestamp] = []
    skipped = 0

    for r in rows:
        if r.get("status") != "CLOSED" or r.get("pnl") is None:
            continue
        try:
            entry_ts = pd.Timestamp(r["opened_at"])
            bars = get_bars(r["symbol"], entry_ts.to_pydatetime())
        except Exception as e:
            log.warning("get_bars failed for trade %s: %s", r["id"], e)
            skipped += 1
            continue

        if len(bars) < MIN_BARS:
            skipped += 1
            continue

        signal = Signal(
            type=SignalType(r["side"]),
            symbol=r["symbol"],
            timestamp=entry_ts.to_pydatetime(),
            price=r["entry_price"],
            stop_loss=r["stop_loss"],
            take_profit=r["take_profit"],
            reason="replay",
        )
        feature_rows.append(build_feature_vector(signal, bars, r["strategy"]))
        labels.append(1 if r["pnl"] > 0 else 0)
        entry_times.append(entry_ts)

    if skipped:
        log.warning("skipped %d trades during dataset build", skipped)

    if not feature_rows:
        empty = pd.DataFrame(columns=list(FEATURE_NAMES))
        return empty, pd.Series([], dtype=int), pd.Series([], dtype="datetime64[ns, UTC]")

    X = pd.DataFrame(feature_rows).reset_index(drop=True)
    X.columns = list(FEATURE_NAMES)  # defensive — preserve order
    y = pd.Series(labels, name="win", dtype=int)
    ts = pd.Series(entry_times, name="entry_time")

    # Sort by entry time so downstream walk-forward code can trust ordering.
    order = ts.argsort().to_numpy()
    return X.iloc[order].reset_index(drop=True), y.iloc[order].reset_index(drop=True), ts.iloc[order].reset_index(drop=True)


def train(
    X: pd.DataFrame,
    y: pd.Series,
    test_size: float = 0.2,
    random_state: int = 7,
    params: dict | None = None,
):
    """Fit an XGBClassifier. Returns (model, TrainingReport)."""
    from sklearn.metrics import accuracy_score, roc_auc_score
    from sklearn.model_selection import train_test_split
    from xgboost import XGBClassifier

    if len(X) < 10:
        raise ValueError(f"need at least 10 labeled samples, got {len(X)}")
    if y.nunique() < 2:
        raise ValueError("training data has only one class — can't train a classifier")

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=test_size, random_state=random_state, stratify=y,
    )

    default_params = dict(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.9,
        colsample_bytree=0.9,
        eval_metric="logloss",
        random_state=random_state,
    )
    default_params.update(params or {})

    model = XGBClassifier(**default_params)
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]
    acc = float(accuracy_score(y_test, y_pred))
    try:
        auc = float(roc_auc_score(y_test, y_proba))
    except ValueError:
        auc = float("nan")  # single-class test split

    importances = model.feature_importances_
    importance_map = {
        name: float(score) for name, score in zip(FEATURE_NAMES, importances)
    }

    report = TrainingReport(
        n_samples=len(X),
        n_wins=int(y.sum()),
        n_losses=int((1 - y).sum()),
        test_accuracy=acc,
        test_auc=auc,
        feature_importance=importance_map,
    )
    return model, report


def walk_forward_splits(
    n_samples: int,
    n_folds: int = 5,
    min_train_size: int | None = None,
    window: str = "expanding",
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Yield (train_idx, test_idx) pairs for time-ordered cross-validation.

    `window="expanding"` (default) grows the train set each fold — more data
    each time, which usually helps. `window="sliding"` keeps the train window
    fixed; pick it when you suspect regime drift makes old data actively
    harmful rather than just less informative.
    """
    if n_folds < 2:
        raise ValueError("need at least 2 folds")
    if n_samples < n_folds + 1:
        raise ValueError(f"need at least {n_folds + 1} samples for {n_folds} folds")

    # Reserve the first slab for the initial train set, split the rest into folds.
    if min_train_size is None:
        min_train_size = max(n_samples // (n_folds + 1), n_folds)
    if min_train_size >= n_samples:
        raise ValueError(f"min_train_size={min_train_size} leaves no test data")

    test_size = max((n_samples - min_train_size) // n_folds, 1)

    for k in range(n_folds):
        test_start = min_train_size + k * test_size
        test_end = test_start + test_size if k < n_folds - 1 else n_samples
        if test_start >= n_samples:
            break
        if window == "sliding":
            train_start = max(0, test_start - min_train_size)
        else:
            train_start = 0
        train_idx = np.arange(train_start, test_start)
        test_idx = np.arange(test_start, test_end)
        yield train_idx, test_idx


def walk_forward_evaluate(
    X: pd.DataFrame,
    y: pd.Series,
    timestamps: pd.Series,
    n_folds: int = 5,
    min_train_size: int | None = None,
    window: str = "expanding",
    params: dict | None = None,
) -> WalkForwardReport:
    """Time-series CV. Assumes X/y/timestamps are already sorted by time.

    Folds that have only one class in the test set get `auc=nan` for that
    fold — accuracy is still reported, and `mean_auc` ignores the NaNs.
    """
    from sklearn.metrics import accuracy_score, roc_auc_score
    from xgboost import XGBClassifier

    if len(X) != len(y) or len(X) != len(timestamps):
        raise ValueError("X, y, timestamps must have the same length")
    if y.nunique() < 2:
        raise ValueError("training data has only one class — can't train a classifier")

    default_params = dict(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.9,
        colsample_bytree=0.9,
        eval_metric="logloss",
        random_state=7,
    )
    default_params.update(params or {})

    report = WalkForwardReport()
    for k, (train_idx, test_idx) in enumerate(walk_forward_splits(
        n_samples=len(X), n_folds=n_folds,
        min_train_size=min_train_size, window=window,
    )):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

        if y_train.nunique() < 2:
            log.warning("fold %d: train set is single-class — skipping", k)
            continue

        model = XGBClassifier(**default_params)
        model.fit(X_train, y_train)
        y_pred = model.predict(X_test)
        y_proba = model.predict_proba(X_test)[:, 1]
        acc = float(accuracy_score(y_test, y_pred))
        try:
            auc = float(roc_auc_score(y_test, y_proba))
        except ValueError:
            auc = float("nan")

        report.folds.append(FoldResult(
            fold=k,
            train_size=len(train_idx),
            test_size=len(test_idx),
            train_start=pd.Timestamp(timestamps.iloc[train_idx[0]]),
            test_start=pd.Timestamp(timestamps.iloc[test_idx[0]]),
            test_end=pd.Timestamp(timestamps.iloc[test_idx[-1]]),
            accuracy=acc,
            auc=auc,
        ))

    return report
