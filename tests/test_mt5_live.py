"""MT5DataFeed + MT5Executor — tested via a fake mt5 module.

Real `MetaTrader5` only runs on Windows, so here we inject a hand-rolled fake
that mirrors the surface we touch. The goal is to verify our adapter, not the
broker's API: request shape, retcode handling, and Order population.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.execution.base import Order, OrderStatus
from src.execution.mt5_live import DEFAULT_MAGIC, MT5DataFeed, MT5Executor
from src.strategies.base import SignalType


# ---------------------------------------------------------------- Fake mt5


@dataclass
class _FakeTick:
    bid: float
    ask: float


@dataclass
class _FakeSymbolInfo:
    filling_mode: int = 1  # FOK supported


@dataclass
class _FakePosition:
    ticket: int
    symbol: str
    type: int
    volume: float
    price_open: float
    sl: float
    tp: float
    time: int
    comment: str
    magic: int


@dataclass
class _FakeAccountInfo:
    balance: float = 10_000.0


@dataclass
class _FakeOrderResult:
    retcode: int
    order: int = 0
    deal: int = 0
    price: float = 0.0
    comment: str = ""


class _FakeMT5:
    """Just enough of the mt5 surface for our adapters."""

    # Constants
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 2
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    TRADE_RETCODE_DONE = 10009

    def __init__(self) -> None:
        self.ticks: dict[str, _FakeTick] = {"EURUSD": _FakeTick(bid=1.1000, ask=1.1002)}
        self.symbol_infos: dict[str, _FakeSymbolInfo] = {"EURUSD": _FakeSymbolInfo()}
        self.positions: list[_FakePosition] = []
        self.account = _FakeAccountInfo()
        self.order_send_calls: list[dict] = []
        self.next_retcode = self.TRADE_RETCODE_DONE
        self.next_ticket = 5000
        self._last_error = (0, "ok")
        self.rates_data: np.ndarray | None = None

    # --- reads
    def symbol_info_tick(self, symbol):
        return self.ticks.get(symbol)

    def symbol_info(self, symbol):
        return self.symbol_infos.get(symbol)

    def symbol_select(self, symbol, enable):
        # Mirror the real API: returns True if the symbol exists on this
        # "server" (we use the symbol_infos dict as the universe).
        return symbol in self.symbol_infos

    def account_info(self):
        return self.account

    def positions_get(self, ticket=None, symbol=None):
        if ticket is not None:
            return tuple(p for p in self.positions if p.ticket == ticket)
        if symbol is not None:
            return tuple(p for p in self.positions if p.symbol == symbol)
        return list(self.positions)

    def last_error(self):
        return self._last_error

    def copy_rates_from_pos(self, symbol, tf, start, count):
        return self.rates_data

    # --- writes
    def order_send(self, request: dict):
        self.order_send_calls.append(request)
        if self.next_retcode != self.TRADE_RETCODE_DONE:
            return _FakeOrderResult(retcode=self.next_retcode, comment="rejected")
        ticket = self.next_ticket
        self.next_ticket += 1
        action = request.get("action")
        if action == self.TRADE_ACTION_SLTP:
            pos_ticket = request.get("position")
            for p in self.positions:
                if p.ticket == pos_ticket:
                    p.sl = request.get("sl", p.sl)
                    p.tp = request.get("tp", p.tp)
            return _FakeOrderResult(retcode=self.TRADE_RETCODE_DONE, order=pos_ticket, price=0.0)

        # Simulate opening a position on a successful buy/sell.
        if action == self.TRADE_ACTION_DEAL and "position" not in request:
            side = self.POSITION_TYPE_BUY if request["type"] == self.ORDER_TYPE_BUY else self.POSITION_TYPE_SELL
            self.positions.append(_FakePosition(
                ticket=ticket,
                symbol=request["symbol"],
                type=side,
                volume=request["volume"],
                price_open=request["price"],
                sl=request.get("sl", 0.0),
                tp=request.get("tp", 0.0),
                time=int(datetime.now(timezone.utc).timestamp()),
                comment=request.get("comment", ""),
                magic=request.get("magic", 0),
            ))
        else:
            # Close path — remove the referenced position.
            pos_ticket = request.get("position")
            self.positions = [p for p in self.positions if p.ticket != pos_ticket]
        return _FakeOrderResult(retcode=self.TRADE_RETCODE_DONE, order=ticket, price=request["price"])


# ---------------------------------------------------------------- Fixtures


@pytest.fixture
def fake_mt5():
    return _FakeMT5()


@pytest.fixture
def executor(fake_mt5):
    return MT5Executor(mt5_module=fake_mt5, magic=DEFAULT_MAGIC)


def _buy_order(symbol: str = "EURUSD") -> Order:
    return Order(
        id=0,
        symbol=symbol,
        side=SignalType.BUY,
        lot_size=0.1,
        entry_price=1.1002,
        stop_loss=1.0950,
        take_profit=1.1100,
        opened_at=datetime.now(timezone.utc),
        strategy="ma_crossover",
    )


# ---------------------------------------------------------------- DataFeed


def test_data_feed_renames_and_orders_columns(fake_mt5):
    # MT5 structured-array-like payload; pandas converts record arrays seamlessly.
    now_ts = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
    dtype = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"),
             ("close", "<f8"), ("tick_volume", "<i8"), ("spread", "<i4"), ("real_volume", "<i8")]
    rows = [(now_ts + i * 900, 1.1 + 0.0001 * i, 1.11, 1.09, 1.1 + 0.00005 * i, 100, 2, 0)
            for i in range(50)]
    fake_mt5.rates_data = np.array(rows, dtype=dtype)

    feed = MT5DataFeed(mt5_module=fake_mt5)
    bars = feed.latest_bars("EURUSD", "M15", 50)

    assert list(bars.columns) == ["open", "high", "low", "close", "volume"]
    assert len(bars) == 50
    assert isinstance(bars.index, pd.DatetimeIndex)


def test_data_feed_raises_when_no_rates(fake_mt5):
    fake_mt5.rates_data = None
    feed = MT5DataFeed(mt5_module=fake_mt5)
    with pytest.raises(RuntimeError, match="no rates"):
        feed.latest_bars("EURUSD", "M15", 50)


def test_data_feed_rejects_unknown_timeframe(fake_mt5):
    feed = MT5DataFeed(mt5_module=fake_mt5)
    with pytest.raises(ValueError, match="Unknown timeframe"):
        feed.latest_bars("EURUSD", "M7", 10)


def test_data_feed_raises_without_mt5(monkeypatch):
    from src.execution import mt5_live
    monkeypatch.setattr(mt5_live, "_load_mt5", lambda: None)
    with pytest.raises(RuntimeError, match="MetaTrader5 is not available"):
        MT5DataFeed(mt5_module=None)


# ---------------------------------------------------------------- Executor place


def test_place_sends_market_buy_with_ask_price(fake_mt5, executor):
    order = executor.place(_buy_order())

    assert order.status == OrderStatus.OPEN
    assert order.broker_ticket is not None
    assert order.entry_price == 1.1002  # ask

    req = fake_mt5.order_send_calls[-1]
    assert req["action"] == fake_mt5.TRADE_ACTION_DEAL
    assert req["type"] == fake_mt5.ORDER_TYPE_BUY
    assert req["price"] == 1.1002
    assert req["symbol"] == "EURUSD"
    assert req["magic"] == DEFAULT_MAGIC
    # MT5 caps comments at 31 chars, so we ship a branded short form
    # ("AG · MAcross · B") instead of the bare strategy name. The
    # reverse-map in MT5Executor decodes it back to "ma_crossover" on
    # open_orders().
    assert req["comment"].startswith("AG · ")
    assert "MAcross" in req["comment"]


def test_place_sell_uses_bid(fake_mt5, executor):
    o = _buy_order()
    o.side = SignalType.SELL
    executor.place(o)
    req = fake_mt5.order_send_calls[-1]
    assert req["type"] == fake_mt5.ORDER_TYPE_SELL
    assert req["price"] == 1.1000  # bid


def test_place_handles_rejection(fake_mt5, executor):
    fake_mt5.next_retcode = 10004  # TRADE_RETCODE_REQUOTE etc.
    order = executor.place(_buy_order())
    assert order.status == OrderStatus.REJECTED
    assert "10004" in order.close_reason


def test_place_handles_order_send_returning_none(fake_mt5, executor):
    fake_mt5.order_send = lambda req: None  # simulate API failure
    fake_mt5._last_error = (1, "disconnected")
    order = executor.place(_buy_order())
    assert order.status == OrderStatus.REJECTED
    assert "None" in order.close_reason


def test_place_rejects_when_tick_unavailable(fake_mt5, executor):
    fake_mt5.ticks.pop("EURUSD")
    order = executor.place(_buy_order())
    assert order.status == OrderStatus.REJECTED
    assert "symbol_info_tick" in order.close_reason


# ---------------------------------------------------------------- Executor close


def test_close_sends_opposite_side_and_marks_closed(fake_mt5, executor):
    opened = executor.place(_buy_order())
    assert opened.broker_ticket is not None
    # Simulate the price moving up so close at the bid locks in profit.
    fake_mt5.ticks["EURUSD"] = _FakeTick(bid=1.1050, ask=1.1052)

    closed = executor.close(opened, "target")

    assert closed.status == OrderStatus.CLOSED
    assert closed.close_reason == "target"
    assert closed.exit_price == 1.1050
    # For a 0.1-lot BUY from 1.1002 → 1.1050 (~48 pips), pnl should be positive.
    assert closed.pnl > 0

    req = fake_mt5.order_send_calls[-1]
    assert req["type"] == fake_mt5.ORDER_TYPE_SELL
    assert req["position"] == opened.broker_ticket


def test_close_without_broker_ticket_is_rejected(executor):
    order = _buy_order()
    order.broker_ticket = None
    closed = executor.close(order, "target")
    assert closed.status == OrderStatus.REJECTED


def test_close_handles_rejection(fake_mt5, executor):
    opened = executor.place(_buy_order())
    fake_mt5.next_retcode = 10019  # TRADE_RETCODE_NO_MONEY
    closed = executor.close(opened, "target")
    assert closed.status == OrderStatus.REJECTED
    assert "10019" in closed.close_reason


# ---------------------------------------------------------------- Listing


def test_open_orders_filters_by_magic(fake_mt5, executor):
    executor.place(_buy_order())

    # Foreign position with a different magic — must be filtered out.
    fake_mt5.positions.append(_FakePosition(
        ticket=9999, symbol="EURUSD", type=fake_mt5.POSITION_TYPE_BUY,
        volume=0.5, price_open=1.0, sl=0.0, tp=0.0,
        time=int(datetime.now(timezone.utc).timestamp()),
        comment="manual", magic=DEFAULT_MAGIC + 1,
    ))

    orders = executor.open_orders()
    assert len(orders) == 1
    assert orders[0].symbol == "EURUSD"
    assert orders[0].strategy == "ma_crossover"
    assert orders[0].side == SignalType.BUY


def test_open_orders_applies_symbols_filter(fake_mt5):
    ex = MT5Executor(mt5_module=fake_mt5, symbols_filter=["GBPUSD"])
    ex.place(_buy_order())  # EURUSD
    assert ex.open_orders() == []


def test_account_balance_reads_from_mt5(fake_mt5, executor):
    fake_mt5.account.balance = 12_345.67
    assert executor.account_balance() == 12_345.67


# ---------------------------------------------------------------- Modify


def test_modify_sends_sltp_action_and_updates_position(fake_mt5, executor):
    opened = executor.place(_buy_order())
    modified = executor.modify(opened, stop_loss=1.0980, take_profit=1.1200)

    req = fake_mt5.order_send_calls[-1]
    assert req["action"] == fake_mt5.TRADE_ACTION_SLTP
    assert req["position"] == opened.broker_ticket
    assert req["sl"] == pytest.approx(1.0980)
    assert req["tp"] == pytest.approx(1.1200)
    assert modified.stop_loss == pytest.approx(1.0980)

    # Position on the broker side should reflect the new levels too.
    position = [p for p in fake_mt5.positions if p.ticket == opened.broker_ticket][0]
    assert position.sl == pytest.approx(1.0980)


def test_modify_without_broker_ticket_is_noop(executor):
    order = _buy_order()
    order.broker_ticket = None
    out = executor.modify(order, stop_loss=1.099)
    # No rejection assertion — just that the original order comes back unchanged.
    assert out.stop_loss == 1.0950


def test_modify_handles_broker_rejection(fake_mt5, executor):
    opened = executor.place(_buy_order())
    original_sl = opened.stop_loss
    fake_mt5.next_retcode = 10019
    result = executor.modify(opened, stop_loss=1.0995)
    # On rejection, the local order keeps the old SL.
    assert result.stop_loss == pytest.approx(original_sl)


# ---------------------------------------------------------------- Filling mode


def test_filling_mode_falls_back_to_ioc_when_only_ioc_supported(fake_mt5, executor):
    fake_mt5.symbol_infos["EURUSD"].filling_mode = 2  # IOC only
    executor.place(_buy_order())
    req = fake_mt5.order_send_calls[-1]
    assert req["type_filling"] == fake_mt5.ORDER_FILLING_IOC


def test_executor_raises_without_mt5(monkeypatch):
    from src.execution import mt5_live
    monkeypatch.setattr(mt5_live, "_load_mt5", lambda: None)
    with pytest.raises(RuntimeError, match="MetaTrader5 is not available"):
        MT5Executor(mt5_module=None)
