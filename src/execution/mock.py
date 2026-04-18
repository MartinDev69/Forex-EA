"""Mock data feed and executor for local development.

MockDataFeed generates a random-walk OHLC series on demand — useful for
wiring the bot end-to-end without a live broker. MockExecutor records
orders in memory and simulates SL/TP fills on synthetic follow-up bars.
"""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import count

import numpy as np
import pandas as pd

from src.strategies.base import SignalType

from .base import DataFeed, Executor, Order, OrderStatus


class MockDataFeed:
    """Generates a deterministic random-walk OHLC series per (symbol, timeframe).

    Bars are cached so repeat calls return a superset of earlier calls —
    simulating a real feed where older bars don't change and new ones append.
    """

    def __init__(self, seed: int = 42, start_price: float = 1.1000) -> None:
        self._seed = seed
        self._start_price = start_price
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}

    def latest_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        key = (symbol, timeframe)
        if key not in self._cache or len(self._cache[key]) < count:
            self._cache[key] = self._generate(symbol, timeframe, max(count, 500))
        return self._cache[key].tail(count).copy()

    def _generate(self, symbol: str, timeframe: str, n: int) -> pd.DataFrame:
        rng = np.random.default_rng(abs(hash((self._seed, symbol, timeframe))) % (2**32))
        steps = rng.normal(0, 0.0005, n)
        close = self._start_price + np.cumsum(steps)
        high = close + rng.uniform(0, 0.0008, n)
        low = close - rng.uniform(0, 0.0008, n)
        open_ = np.concatenate([[close[0]], close[:-1]])
        minutes = _minutes_for(timeframe)
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        idx = pd.date_range(end=end, periods=n, freq=f"{minutes}min")
        return pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": 100},
            index=idx,
        )


class MockExecutor:
    def __init__(self, starting_balance: float = 10_000.0) -> None:
        self.balance = starting_balance
        self._orders: list[Order] = []
        self._ids = count(1)

    def account_balance(self) -> float:
        return self.balance

    def place(self, order: Order) -> Order:
        order.id = next(self._ids)
        order.status = OrderStatus.OPEN
        self._orders.append(order)
        return order

    def close(self, order: Order, reason: str) -> Order:
        order.status = OrderStatus.CLOSED
        order.closed_at = datetime.now(timezone.utc)
        order.exit_price = order.take_profit if reason == "target" else order.stop_loss
        order.close_reason = reason
        order.pnl = _pnl(order)
        self.balance += order.pnl
        return order

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders if o.status == OrderStatus.OPEN]


def _pnl(order: Order) -> float:
    # Rough 4-decimal-pair approximation: $10 per pip per lot.
    pip_value = 10.0 * order.lot_size
    diff = (order.exit_price or 0.0) - order.entry_price
    if order.side == SignalType.SELL:
        diff = -diff
    pips = diff * 10_000
    return pips * pip_value


def _minutes_for(timeframe: str) -> int:
    mapping = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}
    return mapping.get(timeframe.upper(), 15)
