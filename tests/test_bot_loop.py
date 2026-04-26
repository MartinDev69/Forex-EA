"""End-to-end test of the bot event loop.

Verifies: strategy signal → RiskManager approval → Executor.place → journal
insert. Uses MockDataFeed so nothing touches a broker.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.bot import Bot, BotConfig
from src.execution.base import Order, OrderStatus
from src.execution.journal import TradeJournal
from src.execution.mock import MockExecutor
from src.execution.stops import StopManager, StopPolicy
from src.execution.strategy_toggles import StrategyToggleStore
from src.risk.risk_manager import RiskLimits, RiskManager
from src.strategies.base import Signal, SignalType, Strategy


class AlwaysBuyStrategy(Strategy):
    """Deterministic: always emits a BUY signal."""
    name = "always_buy"

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        last = ohlc.iloc[-1]
        price = float(last["close"])
        return Signal(
            type=SignalType.BUY,
            symbol=self.symbol,
            timestamp=ohlc.index[-1],
            price=price,
            stop_loss=price - 0.0050,
            take_profit=price + 0.0100,
            reason="test",
        )


class _FixedFeed:
    def __init__(self, ohlc: pd.DataFrame) -> None:
        self._ohlc = ohlc

    def latest_bars(self, symbol, timeframe, count):
        return self._ohlc.tail(count).copy()


def _sample_ohlc(bars: int = 100, base: float = 1.1000) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    close = base + np.cumsum(rng.normal(0, 0.0005, bars))
    high = close + 0.0008
    low = close - 0.0008
    idx = pd.date_range("2024-01-01", periods=bars, freq="15min")
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": 100},
        index=idx,
    )


def test_tick_places_order_and_journals_it(tmp_path: Path):
    ohlc = _sample_ohlc()
    feed = _FixedFeed(ohlc)
    executor = MockExecutor(starting_balance=10_000)
    risk = RiskManager(RiskLimits(risk_per_trade=0.01, max_open_trades=5))
    journal = TradeJournal(tmp_path / "trades.db")

    bot = Bot(
        config=BotConfig(symbols=["EURUSD"], timeframe="M15", poll_interval_s=1),
        strategies={"EURUSD": [AlwaysBuyStrategy("EURUSD")]},
        data_feed=feed,
        executor=executor,
        risk_manager=risk,
        journal=journal,
    )

    acted = bot.tick()

    assert acted == 1
    open_orders = executor.open_orders()
    assert len(open_orders) == 1
    assert open_orders[0].symbol == "EURUSD"
    assert open_orders[0].side == SignalType.BUY
    assert open_orders[0].lot_size > 0

    recent = journal.recent()
    assert len(recent) == 1
    assert recent[0]["strategy"] == "always_buy"
    assert recent[0]["status"] == "OPEN"


def test_tick_respects_risk_block(tmp_path: Path):
    ohlc = _sample_ohlc()
    feed = _FixedFeed(ohlc)
    executor = MockExecutor(starting_balance=10_000)
    risk = RiskManager(RiskLimits(max_open_trades=0))  # blocks everything
    journal = TradeJournal(tmp_path / "trades.db")

    bot = Bot(
        config=BotConfig(symbols=["EURUSD"]),
        strategies={"EURUSD": [AlwaysBuyStrategy("EURUSD")]},
        data_feed=feed,
        executor=executor,
        risk_manager=risk,
        journal=journal,
    )

    assert bot.tick() == 0
    assert executor.open_orders() == []


def test_tick_doesnt_fire_twice_on_same_bar(tmp_path: Path):
    ohlc = _sample_ohlc()
    feed = _FixedFeed(ohlc)
    executor = MockExecutor(starting_balance=10_000)
    risk = RiskManager(RiskLimits(risk_per_trade=0.01, max_open_trades=5))
    journal = TradeJournal(tmp_path / "trades.db")

    bot = Bot(
        config=BotConfig(symbols=["EURUSD"]),
        strategies={"EURUSD": [AlwaysBuyStrategy("EURUSD")]},
        data_feed=feed,
        executor=executor,
        risk_manager=risk,
        journal=journal,
    )

    assert bot.tick() == 1
    assert bot.tick() == 0  # same bar, should not re-fire
    assert len(executor.open_orders()) == 1


def test_close_on_target_hits_journal(tmp_path: Path):
    # Build bars where the last bar's high exceeds take_profit.
    idx = pd.date_range("2024-01-01", periods=60, freq="15min")
    close = np.full(60, 1.1000)
    high = np.full(60, 1.1010)
    low = np.full(60, 1.0990)
    # Last bar spikes high well above the TP.
    high[-1] = 1.1200
    ohlc = pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": 100},
        index=idx,
    )
    feed = _FixedFeed(ohlc)
    executor = MockExecutor(starting_balance=10_000)
    risk = RiskManager(RiskLimits(risk_per_trade=0.01, max_open_trades=5))
    journal = TradeJournal(tmp_path / "trades.db")

    bot = Bot(
        config=BotConfig(symbols=["EURUSD"]),
        strategies={"EURUSD": [AlwaysBuyStrategy("EURUSD")]},
        data_feed=feed,
        executor=executor,
        risk_manager=risk,
        journal=journal,
    )

    # Open a trade
    bot.tick()
    assert len(executor.open_orders()) == 1

    # Manually trigger close evaluation by re-ticking (same bar, so no new signal,
    # but _should_close_order runs first and will close the open trade)
    bot.tick()
    assert len(executor.open_orders()) == 0
    rows = journal.recent()
    assert rows[0]["status"] == "CLOSED"
    assert rows[0]["close_reason"]


def test_disabled_strategy_is_skipped(tmp_path: Path):
    """When the toggle store says a strategy is off, the bot must not fire it."""
    ohlc = _sample_ohlc()
    feed = _FixedFeed(ohlc)
    executor = MockExecutor(starting_balance=10_000)
    risk = RiskManager(RiskLimits(risk_per_trade=0.01, max_open_trades=5))
    journal = TradeJournal(tmp_path / "trades.db")

    toggles = StrategyToggleStore(tmp_path / "trades.db")
    toggles.set("always_buy", False)

    bot = Bot(
        config=BotConfig(symbols=["EURUSD"]),
        strategies={"EURUSD": [AlwaysBuyStrategy("EURUSD")]},
        data_feed=feed,
        executor=executor,
        risk_manager=risk,
        journal=journal,
        toggle_store=toggles,
    )

    assert bot.tick() == 0
    assert executor.open_orders() == []
    assert journal.recent() == []

    # Flip it on — next tick should fire. (Same bar-key was recorded on the prior
    # tick, so we fast-forward the feed to a fresh bar by rebuilding with a
    # different last timestamp.)
    toggles.set("always_buy", True)
    bot.state.last_bar_ts.clear()
    assert bot.tick() == 1
    assert len(executor.open_orders()) == 1


class _MutableFeed:
    """Feed whose last bar is mutable between ticks — lets us simulate price
    movement over time in the same test."""

    def __init__(self, ohlc: pd.DataFrame) -> None:
        self._ohlc = ohlc

    def latest_bars(self, symbol, timeframe, count):
        return self._ohlc.tail(count).copy()

    def append_bar(self, high: float, low: float, close: float) -> None:
        last_ts = self._ohlc.index[-1]
        new_ts = last_ts + (self._ohlc.index[-1] - self._ohlc.index[-2])
        row = pd.DataFrame(
            {"open": [close], "high": [high], "low": [low], "close": [close], "volume": [100]},
            index=[new_ts],
        )
        self._ohlc = pd.concat([self._ohlc, row])


def test_stop_manager_moves_sl_to_breakeven_after_one_r(tmp_path: Path):
    ohlc = _sample_ohlc()
    feed = _MutableFeed(ohlc)
    executor = MockExecutor(starting_balance=10_000)
    risk = RiskManager(RiskLimits(risk_per_trade=0.01, max_open_trades=5))
    journal = TradeJournal(tmp_path / "trades.db")

    bot = Bot(
        config=BotConfig(symbols=["EURUSD"]),
        strategies={"EURUSD": [AlwaysBuyStrategy("EURUSD")]},
        data_feed=feed,
        executor=executor,
        risk_manager=risk,
        journal=journal,
        stop_manager=StopManager(StopPolicy(breakeven_trigger_r=1.0, trail_start_r=99)),
    )

    # Tick 1: opens a BUY with 50-pip stop (AlwaysBuyStrategy sets sl = price - 0.005).
    bot.tick()
    order = executor.open_orders()[0]
    entry = order.entry_price
    initial_sl = order.stop_loss
    assert initial_sl == pytest.approx(entry - 0.0050)

    # Tick 2: price pushes +1R above entry on a new bar — SL should move to entry.
    feed.append_bar(high=entry + 0.0060, low=entry + 0.0030, close=entry + 0.0055)
    bot.tick()
    assert order.stop_loss == pytest.approx(entry, abs=1e-6)


def test_stop_manager_trails_after_two_r(tmp_path: Path):
    ohlc = _sample_ohlc()
    feed = _MutableFeed(ohlc)
    executor = MockExecutor(starting_balance=10_000)
    risk = RiskManager(RiskLimits(risk_per_trade=0.01, max_open_trades=5))
    journal = TradeJournal(tmp_path / "trades.db")

    bot = Bot(
        config=BotConfig(symbols=["EURUSD"]),
        strategies={"EURUSD": [AlwaysBuyStrategy("EURUSD")]},
        data_feed=feed,
        executor=executor,
        risk_manager=risk,
        journal=journal,
        stop_manager=StopManager(StopPolicy(
            breakeven_trigger_r=1.0, trail_start_r=2.0, trail_distance_r=1.0,
        )),
    )

    bot.tick()
    order = executor.open_orders()[0]
    entry = order.entry_price

    # Push peak to +3R (0.015 above entry). Trail distance = 1R = 0.005.
    # Expected SL = peak - 1R = entry + 0.010.
    feed.append_bar(high=entry + 0.0150, low=entry + 0.0080, close=entry + 0.0120)
    bot.tick()
    assert order.stop_loss == pytest.approx(entry + 0.0100, abs=1e-6)
