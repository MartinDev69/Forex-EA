"""SignalFilter — XGBoost meta-model that gates strategy signals.

Flow:
  strategy.generate_signal(bars) -> Signal
  SignalFilter.should_take(signal, bars, strategy_name) -> bool
  if True: RiskManager decides lot size and the Executor places the order

The filter is optional. If no model is loaded (or the filter isn't wired into
the Bot at all), every signal passes through unchanged — same as before.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from src.strategies.base import Signal

from .features import FEATURE_NAMES, build_feature_vector, has_enough_bars

log = logging.getLogger(__name__)


class SignalFilter:
    def __init__(self, model=None, threshold: float = 0.55) -> None:
        """`model` is an XGBClassifier (or anything with predict_proba).

        `threshold` is the minimum win-probability required to take the trade.
        Default 0.55 — start selective; tune with the training report.
        """
        self.model = model
        self.threshold = threshold

    # ---------------------------------------------------------------- inference

    def predict_proba(self, signal: Signal, ohlc: pd.DataFrame, strategy_name: str) -> float:
        """Return P(win) in [0, 1]. 0.5 when no model is loaded (neutral)."""
        if self.model is None:
            return 0.5
        if not has_enough_bars(ohlc):
            return 0.5
        features = build_feature_vector(signal, ohlc, strategy_name)
        X = features.to_numpy().reshape(1, -1)
        proba = self.model.predict_proba(X)[0]
        # Binary classifier: column 1 is P(win=1).
        return float(proba[1]) if proba.shape[0] > 1 else float(proba[0])

    def should_take(self, signal: Signal, ohlc: pd.DataFrame, strategy_name: str) -> bool:
        """Allow the signal through iff the model is confident enough.

        No-model mode: returns True (the filter becomes a pass-through so a bot
        started before any training run behaves identically to today).
        """
        if self.model is None:
            return True
        p = self.predict_proba(signal, ohlc, strategy_name)
        allowed = p >= self.threshold
        log.debug(
            "filter %s %s p=%.3f thr=%.3f -> %s",
            strategy_name, signal.symbol, p, self.threshold,
            "TAKE" if allowed else "SKIP",
        )
        return allowed

    # ------------------------------------------------------------------ I/O

    def save(self, path: Path | str) -> None:
        if self.model is None:
            raise ValueError("no model to save")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.model.save_model(str(path))

    @classmethod
    def load(cls, path: Path | str, threshold: float = 0.55) -> "SignalFilter":
        from xgboost import XGBClassifier

        model = XGBClassifier()
        model.load_model(str(Path(path)))
        return cls(model=model, threshold=threshold)

    @property
    def feature_names(self) -> tuple[str, ...]:
        return FEATURE_NAMES
