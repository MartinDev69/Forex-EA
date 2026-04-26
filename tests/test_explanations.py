"""Tests for trade explanation store, bot capture path, and API endpoint."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.explanations.store import TradeExplanation, TradeExplanationStore


def _exp(**overrides) -> TradeExplanation:
    base = dict(
        trade_id=1,
        strategy="ma_crossover",
        symbol="EURUSD",
        side="BUY",
        signal_price=1.1000,
        signal_stop=1.0950,
        signal_target=1.1100,
        risk_reward=2.0,
        stop_distance_pips=50.0,
        lot_size=0.1,
        account_balance=10_000.0,
        opened_at=datetime.now(timezone.utc).isoformat(),
    )
    base.update(overrides)
    return TradeExplanation(**base)


# ---------------------------------------------------------------- store


def test_store_roundtrip(tmp_path):
    store = TradeExplanationStore(tmp_path / "trades.db")
    store.record(_exp(trade_id=42, regime_trend="bull", regime_volatility="low"))
    got = store.get(42)
    assert got is not None
    assert got.strategy == "ma_crossover"
    assert got.regime_trend == "bull"
    assert got.regime_volatility == "low"
    assert got.risk_reward == pytest.approx(2.0)


def test_store_missing_returns_none(tmp_path):
    store = TradeExplanationStore(tmp_path / "trades.db")
    assert store.get(999) is None


def test_store_replace(tmp_path):
    store = TradeExplanationStore(tmp_path / "trades.db")
    store.record(_exp(trade_id=1, lot_size=0.1))
    # The bot may re-record on retry — second write should overwrite.
    store.record(_exp(trade_id=1, lot_size=0.5))
    got = store.get(1)
    assert got.lot_size == pytest.approx(0.5)


def test_ml_filter_passed_tri_state(tmp_path):
    store = TradeExplanationStore(tmp_path / "trades.db")
    # None on the wire should round-trip as None (not False).
    store.record(_exp(trade_id=1, ml_filter_passed=None))
    store.record(_exp(trade_id=2, ml_filter_passed=True))
    store.record(_exp(trade_id=3, ml_filter_passed=False))
    assert store.get(1).ml_filter_passed is None
    assert store.get(2).ml_filter_passed is True
    assert store.get(3).ml_filter_passed is False


# ---------------------------------------------------------------- bot path


def _stub_signal(symbol="EURUSD", side="BUY", price=1.10, sl=1.095, tp=1.110):
    """Cheap signal stub — avoids importing heavy strategy machinery."""
    from src.strategies.base import Signal, SignalType
    return Signal(
        type=SignalType.BUY if side == "BUY" else SignalType.SELL,
        symbol=symbol, timestamp=datetime.now(timezone.utc), price=price,
        stop_loss=sl, take_profit=tp, reason="ma cross above with adx > 25",
    )


def _stub_order(trade_id=1, lot_size=0.1, side="BUY"):
    from src.execution.base import Order, OrderStatus
    from src.strategies.base import SignalType
    return Order(
        id=trade_id, symbol="EURUSD",
        side=SignalType.BUY if side == "BUY" else SignalType.SELL,
        lot_size=lot_size, entry_price=1.10, stop_loss=1.095, take_profit=1.110,
        opened_at=datetime.now(timezone.utc), strategy="ma_crossover",
        status=OrderStatus.OPEN,
    )


class _FakeStrategy:
    name = "ma_crossover"
    preferred_regimes = ()


class _FakeExecutor:
    def __init__(self, balance=10_000.0):
        self._balance = balance

    def account_balance(self):
        return self._balance


def test_bot_records_explanation_with_regime_and_allocator(tmp_path):
    from src.bot import Bot
    from src.regime.classifier import (
        RegimeSnapshot, TrendRegime, VolatilityRegime,
    )

    store = TradeExplanationStore(tmp_path / "trades.db")
    bot = Bot.__new__(Bot)
    bot.explanation_store = store
    bot.executor = _FakeExecutor()
    bot.signal_filter = None  # no filter wired

    regime = RegimeSnapshot(
        trend=TrendRegime.TREND_UP, volatility=VolatilityRegime.LOW,
        adx=28.5, plus_di=22.0, minus_di=10.0,
        atr=0.0010, atr_pct=0.4, timestamp=None,
    )

    bot._record_explanation(
        order=_stub_order(trade_id=7),
        signal=_stub_signal(),
        strategy=_FakeStrategy(),
        regime=regime,
        allocator_role="champion",
        allocator_weight=1.0,
    )

    got = store.get(7)
    assert got is not None
    assert got.strategy == "ma_crossover"
    assert got.regime_trend == "trend_up"
    assert got.regime_label == "trend_up:low"
    assert got.regime_adx == pytest.approx(28.5)
    assert got.allocator_role == "champion"
    assert got.allocator_weight == pytest.approx(1.0)
    assert got.ml_filter_passed is None  # no filter wired
    assert got.risk_reward == pytest.approx(2.0)
    assert "ma cross" in got.notes


def test_bot_no_store_is_no_op(tmp_path):
    from src.bot import Bot
    bot = Bot.__new__(Bot)
    bot.explanation_store = None
    # Even though we don't pass a regime/role, this must not raise.
    bot._record_explanation(
        order=_stub_order(),
        signal=_stub_signal(),
        strategy=_FakeStrategy(),
        regime=None,
        allocator_role="unmanaged",
        allocator_weight=1.0,
    )


def test_bot_unmanaged_allocator_writes_none(tmp_path):
    """When the allocator is off, we shouldn't claim a fake role."""
    from src.bot import Bot

    store = TradeExplanationStore(tmp_path / "trades.db")
    bot = Bot.__new__(Bot)
    bot.explanation_store = store
    bot.executor = _FakeExecutor()
    bot.signal_filter = None

    bot._record_explanation(
        order=_stub_order(trade_id=11),
        signal=_stub_signal(),
        strategy=_FakeStrategy(),
        regime=None,
        allocator_role="unmanaged",
        allocator_weight=1.0,
    )
    got = store.get(11)
    assert got.allocator_role is None
    assert got.allocator_weight is None


# ---------------------------------------------------------------- API


@pytest.fixture
def explain_api(tmp_path):
    from fastapi.testclient import TestClient
    from src.api import auth as auth_module
    from src.api import server as server_module

    db = tmp_path / "trades.db"
    store = TradeExplanationStore(db)
    server_module.explanation_store = store
    server_module.app.dependency_overrides[auth_module.current_user] = lambda: {
        "username": "test", "role": "admin",
    }
    client = TestClient(server_module.app)
    yield client, store
    server_module.app.dependency_overrides.clear()


def test_api_404_for_missing(explain_api):
    client, _ = explain_api
    r = client.get("/trades/9999/explain")
    assert r.status_code == 404


def test_api_returns_explanation(explain_api):
    client, store = explain_api
    store.record(_exp(trade_id=21, regime_trend="bull", regime_label="bull:low",
                      allocator_role="probe", allocator_weight=0.1))
    r = client.get("/trades/21/explain")
    assert r.status_code == 200
    body = r.json()
    assert body["trade_id"] == 21
    assert body["regime_trend"] == "bull"
    assert body["regime_label"] == "bull:low"
    assert body["allocator_role"] == "probe"
    assert body["allocator_weight"] == pytest.approx(0.1)
