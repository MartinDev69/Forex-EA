"""Strategy interface — every strategy produces Signals from OHLC data."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import pandas as pd


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    CLOSE = "CLOSE"


@dataclass
class Signal:
    type: SignalType
    symbol: str
    timestamp: datetime
    price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""
    # Indicator snapshot at the bar that triggered the signal — what the
    # strategy "saw" when it decided. Persisted with the trade so the
    # dashboard can show the user how the bot reached the conclusion.
    # Keys are short (e.g. "rsi", "ema_fast"); values are floats or strings.
    indicators: dict = field(default_factory=dict)


class Strategy(ABC):
    name: str = "base"

    # Regimes (from src.regime.TrendRegime) in which this strategy should fire.
    # Default is regime-agnostic — the bot's regime gate is a no-op unless a
    # subclass narrows this. Use strings, not enums, so strategies don't take a
    # dependency on the regime module.
    preferred_regimes: frozenset[str] = frozenset({"trend_up", "trend_down", "range"})

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    @abstractmethod
    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        """Return a Signal based on the latest bar of `ohlc`.

        `ohlc` is expected to have columns: open, high, low, close, volume
        and a DatetimeIndex.
        """
        raise NotImplementedError
