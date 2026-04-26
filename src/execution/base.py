"""Abstract interfaces for data + order execution.

The bot depends on these protocols, not on MT5 directly. That lets us
run the whole pipeline locally (macOS/Linux) with MockDataFeed/MockExecutor,
and switch to MT5DataFeed/MT5Executor on the Windows VPS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

import pandas as pd

from src.strategies.base import SignalType


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


@dataclass
class Order:
    id: int
    symbol: str
    side: SignalType
    lot_size: float
    entry_price: float
    stop_loss: float
    take_profit: float
    opened_at: datetime
    strategy: str
    status: OrderStatus = OrderStatus.OPEN
    exit_price: float | None = None
    closed_at: datetime | None = None
    pnl: float = 0.0
    close_reason: str = ""
    broker_ticket: int | None = None
    extra: dict = field(default_factory=dict)


class DataFeed(Protocol):
    def latest_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame: ...


class Executor(Protocol):
    def account_balance(self) -> float: ...
    def place(self, order: Order) -> Order: ...
    def close(self, order: Order, reason: str) -> Order: ...
    def open_orders(self) -> list[Order]: ...
    def modify(self, order: Order, stop_loss: float | None = None,
               take_profit: float | None = None) -> Order: ...
