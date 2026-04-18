"""Strategy interface — every strategy produces Signals from OHLC data."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
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


class Strategy(ABC):
    name: str = "base"

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    @abstractmethod
    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        """Return a Signal based on the latest bar of `ohlc`.

        `ohlc` is expected to have columns: open, high, low, close, volume
        and a DatetimeIndex.
        """
        raise NotImplementedError
