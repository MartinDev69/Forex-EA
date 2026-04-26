"""Tests for the execution-quality (fills) log."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.execution.fills import (
    Fill,
    FillStore,
    pip_size,
    signed_slippage_pips,
)


# ---------------------------------------------------------------- helpers


def _make_fill(
    *,
    symbol: str = "EURUSD",
    side: str = "BUY",
    event: str = "OPEN",
    requested: float = 1.10000,
    filled: float | None = 1.10002,
    slippage: float | None = 0.2,
    latency: float = 12.0,
    status: str = "FILLED",
    when: datetime | None = None,
) -> Fill:
    return Fill(
        trade_id=1,
        symbol=symbol,
        side=side,
        event=event,  # type: ignore[arg-type]
        requested_price=requested,
        filled_price=filled,
        slippage_pips=slippage,
        latency_ms=latency,
        broker_ticket=999,
        status=status,  # type: ignore[arg-type]
        reason=None,
        filled_at=when or datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------- pip_size + slippage


def test_pip_size_fx_majors():
    assert pip_size("EURUSD") == 0.0001


def test_pip_size_jpy_pair():
    assert pip_size("USDJPY") == 0.01


def test_pip_size_gold():
    assert pip_size("XAUUSD") == 0.01


def test_slippage_buy_paid_more_is_adverse():
    # BUY at 1.1000, filled at 1.1002 → 2 pips against the trader.
    assert signed_slippage_pips("EURUSD", "BUY", 1.10000, 1.10020) == pytest.approx(2.0)


def test_slippage_sell_received_less_is_adverse():
    # SELL at 1.1000, filled at 1.0998 → 2 pips against the trader.
    assert signed_slippage_pips("EURUSD", "SELL", 1.10000, 1.09980) == pytest.approx(2.0)


def test_slippage_buy_paid_less_is_favorable():
    assert signed_slippage_pips("EURUSD", "BUY", 1.10000, 1.09995) == pytest.approx(-0.5)


def test_slippage_jpy_uses_jpy_pip():
    # USDJPY at 150.00 → 150.10 = 10 pips for JPY pairs.
    assert signed_slippage_pips("USDJPY", "BUY", 150.00, 150.10) == pytest.approx(10.0)


# ---------------------------------------------------------------- store


def test_store_record_and_recent(tmp_path):
    store = FillStore(tmp_path / "trades.db")
    store.record(_make_fill())
    rows = store.recent()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "EURUSD"
    assert rows[0]["status"] == "FILLED"


def test_store_recent_orders_newest_first(tmp_path):
    store = FillStore(tmp_path / "trades.db")
    base = datetime(2024, 6, 1, tzinfo=timezone.utc)
    store.record(_make_fill(when=base, requested=1.1))
    store.record(_make_fill(when=base + timedelta(seconds=10), requested=1.2))
    rows = store.recent()
    assert rows[0]["requested_price"] == pytest.approx(1.2)
    assert rows[1]["requested_price"] == pytest.approx(1.1)


def test_store_records_rejected_fill(tmp_path):
    store = FillStore(tmp_path / "trades.db")
    store.record(_make_fill(filled=None, slippage=None, status="REJECTED"))
    rows = store.recent()
    assert rows[0]["status"] == "REJECTED"
    assert rows[0]["filled_price"] is None
    assert rows[0]["slippage_pips"] is None


# ---------------------------------------------------------------- stats


def test_stats_groups_by_symbol(tmp_path):
    store = FillStore(tmp_path / "trades.db")
    for slip in (0.5, 1.0, 1.5):
        store.record(_make_fill(symbol="EURUSD", slippage=slip, latency=10))
    for slip in (0.0, 0.0):
        store.record(_make_fill(symbol="GBPUSD", slippage=slip, latency=20))

    stats = store.stats()
    by_sym = {s.symbol: s for s in stats}
    assert by_sym["EURUSD"].fill_count == 3
    assert by_sym["EURUSD"].avg_slippage_pips == pytest.approx(1.0)
    assert by_sym["EURUSD"].max_slippage_pips == pytest.approx(1.5)
    assert by_sym["GBPUSD"].fill_count == 2
    assert by_sym["GBPUSD"].avg_slippage_pips == pytest.approx(0.0)


def test_stats_excludes_rejected_from_slippage(tmp_path):
    store = FillStore(tmp_path / "trades.db")
    store.record(_make_fill(slippage=2.0))  # filled
    store.record(_make_fill(filled=None, slippage=None, status="REJECTED"))
    stats = store.stats()
    assert stats[0].fill_count == 1
    assert stats[0].rejected_count == 1
    assert stats[0].avg_slippage_pips == pytest.approx(2.0)


def test_stats_window_hours_filters(tmp_path):
    store = FillStore(tmp_path / "trades.db")
    now = datetime.now(timezone.utc)
    store.record(_make_fill(when=now - timedelta(hours=48), slippage=10.0))
    store.record(_make_fill(when=now, slippage=1.0))
    stats = store.stats(since_hours=24)
    assert stats[0].fill_count == 1
    assert stats[0].avg_slippage_pips == pytest.approx(1.0)


# ---------------------------------------------------------------- API


@pytest.fixture
def fill_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src.api import auth as auth_module
    from src.api import server as server_module

    store = FillStore(tmp_path / "trades.db")
    monkeypatch.setattr(server_module, "fill_store", store)
    server_module._fill_stats_cache["value"] = None
    server_module._fill_stats_cache["expires_at"] = 0.0
    server_module._fill_stats_cache["window"] = None
    server_module.app.dependency_overrides[auth_module.current_user] = lambda: {
        "username": "test", "role": "admin",
    }
    client = TestClient(server_module.app)
    yield client, store
    server_module.app.dependency_overrides.clear()


def test_fills_endpoint_empty(fill_api):
    client, _ = fill_api
    r = client.get("/fills")
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_fills_endpoint_returns_recent(fill_api):
    client, store = fill_api
    store.record(_make_fill(symbol="EURUSD"))
    r = client.get("/fills?limit=10")
    body = r.json()
    assert body["count"] == 1
    assert body["fills"][0]["symbol"] == "EURUSD"


def test_fills_stats_endpoint(fill_api):
    client, store = fill_api
    store.record(_make_fill(symbol="EURUSD", slippage=0.5))
    store.record(_make_fill(symbol="EURUSD", slippage=1.5))
    r = client.get("/fills/stats?window_hours=72")
    body = r.json()
    assert body["window_hours"] == 72
    assert body["symbols"][0]["fill_count"] == 2
    assert body["symbols"][0]["avg_slippage_pips"] == pytest.approx(1.0)


# ---------------------------------------------------------------- bot wiring


def test_bot_records_fill_when_store_provided(tmp_path):
    """End-to-end: Bot._record_fill writes to the store on a successful place()."""
    from src.bot import Bot
    from src.execution.base import Order, OrderStatus
    from src.strategies.base import SignalType

    store = FillStore(tmp_path / "trades.db")
    bot = Bot.__new__(Bot)
    bot.fill_store = store

    order = Order(
        id=1, symbol="EURUSD", side=SignalType.BUY, lot_size=0.1,
        entry_price=1.10003, stop_loss=1.09800, take_profit=1.10500,
        opened_at=datetime.now(timezone.utc), strategy="ma_crossover",
        status=OrderStatus.OPEN, broker_ticket=42,
    )
    bot._record_fill(order, "OPEN", requested_price=1.10000, latency_ms=8.5)
    rows = store.recent()
    assert len(rows) == 1
    assert rows[0]["status"] == "FILLED"
    assert rows[0]["slippage_pips"] == pytest.approx(0.3)
    assert rows[0]["latency_ms"] == pytest.approx(8.5)


def test_bot_no_store_is_no_op(tmp_path):
    from src.bot import Bot
    from src.execution.base import Order, OrderStatus
    from src.strategies.base import SignalType

    bot = Bot.__new__(Bot)
    bot.fill_store = None
    order = Order(
        id=1, symbol="EURUSD", side=SignalType.BUY, lot_size=0.1,
        entry_price=1.1, stop_loss=1.09, take_profit=1.11,
        opened_at=datetime.now(timezone.utc), strategy="x",
        status=OrderStatus.OPEN,
    )
    # Should not raise even without a store.
    bot._record_fill(order, "OPEN", requested_price=1.1, latency_ms=1.0)


def test_bot_records_rejected_fill(tmp_path):
    from src.bot import Bot
    from src.execution.base import Order, OrderStatus
    from src.strategies.base import SignalType

    store = FillStore(tmp_path / "trades.db")
    bot = Bot.__new__(Bot)
    bot.fill_store = store
    order = Order(
        id=0, symbol="GBPUSD", side=SignalType.SELL, lot_size=0.05,
        entry_price=1.25000, stop_loss=1.26000, take_profit=1.24000,
        opened_at=datetime.now(timezone.utc), strategy="rsi_mean_reversion",
        status=OrderStatus.REJECTED, close_reason="retcode=10018",
    )
    bot._record_fill(order, "OPEN", requested_price=1.25000, latency_ms=42.0)
    rows = store.recent()
    assert rows[0]["status"] == "REJECTED"
    assert rows[0]["filled_price"] is None
