from types import SimpleNamespace

import pytest

from src.risk.position_sizing import (
    PipResolver,
    lot_size_from_risk,
    pip_size,
    pip_value,
    set_resolver,
)
from src.risk.risk_manager import RiskLimits, RiskManager


@pytest.fixture(autouse=True)
def _clear_pip_resolver():
    """Each test runs with a clean (no-resolver) state so the fallback table
    drives unless the test installs a fake resolver itself."""
    set_resolver(None)
    yield
    set_resolver(None)


class _FakeMT5:
    """Stub of the MT5 module exposing just symbol_info + symbol_select."""

    def __init__(self, table: dict[str, dict]) -> None:
        self._table = table
        self.selected: list[str] = []

    def symbol_select(self, symbol, enable):
        self.selected.append(symbol)
        return True

    def symbol_info(self, symbol):
        row = self._table.get(symbol)
        if row is None:
            return None
        return SimpleNamespace(**row)


def test_pip_value_defaults():
    assert pip_value("EURUSD") == 10.0
    assert pip_value("USDJPY") == 9.0


def test_lot_size_basic():
    # 10000 balance, 1% risk, 50 pip stop, EURUSD → $100 / (50 × $10) = 0.2 lots
    lots = lot_size_from_risk(10_000, 0.01, 50, "EURUSD")
    assert lots == pytest.approx(0.2, abs=0.01)


def test_lot_size_skips_when_below_min():
    # $100 balance, 1% risk = $1 budget; 200-pip stop on EURUSD is
    # $2000 per lot, so the math wants 0.0005 lots — well under the
    # 0.01-lot broker minimum. Returning min_lot would deliver ~20×
    # the intended risk; we return 0 instead and the bot skips.
    lots = lot_size_from_risk(100, 0.01, 200, "EURUSD")
    assert lots == 0.0


def test_risk_manager_blocks_over_max_trades():
    rm = RiskManager(RiskLimits(max_open_trades=1))
    rm.register_trade_opened(risk_pct=0.01)
    decision = rm.evaluate(10_000, 50, "EURUSD", lot_sizer=lot_size_from_risk)
    assert not decision.approved
    assert "max open trades" in decision.reason


def test_risk_manager_blocks_low_balance():
    rm = RiskManager(RiskLimits(min_balance=500))
    decision = rm.evaluate(100, 50, "EURUSD", lot_sizer=lot_size_from_risk)
    assert not decision.approved


def test_risk_manager_daily_loss_circuit():
    rm = RiskManager(RiskLimits(max_daily_loss_pct=0.05))
    rm.register_trade_closed(risk_pct=0.01, pnl=-600)  # 6% loss on 10k
    decision = rm.evaluate(10_000, 50, "EURUSD", lot_sizer=lot_size_from_risk)
    assert not decision.approved
    assert "circuit breaker" in decision.reason


def test_risk_manager_approves_clean_request():
    rm = RiskManager(RiskLimits())
    decision = rm.evaluate(10_000, 50, "EURUSD", lot_sizer=lot_size_from_risk)
    assert decision.approved
    assert decision.lot_size and decision.lot_size > 0


# --------------------------------------------------------------- PipResolver


def test_resolver_pip_size_from_mt5_point():
    # EURUSDm on Exness reports point=0.00001; pip = 10 points = 0.0001.
    fake = _FakeMT5({
        "EURUSDm": {"point": 0.00001, "trade_tick_value": 1.0, "trade_tick_size": 0.00001},
    })
    set_resolver(PipResolver(fake))
    assert pip_size("EURUSDm") == pytest.approx(0.0001)


def test_resolver_pip_size_for_deriv_volatility():
    # Deriv "Volatility 75 Index" point is typically 0.01 → pip 0.1.
    # The hardcoded fallback would have returned 0.0001 — wrong by 1000x.
    fake = _FakeMT5({
        "Volatility 75 Index": {
            "point": 0.01, "trade_tick_value": 0.001, "trade_tick_size": 0.01,
        },
    })
    set_resolver(PipResolver(fake))
    assert pip_size("Volatility 75 Index") == pytest.approx(0.1)


def test_resolver_pip_value_uses_mt5_tick_value():
    # pip = 10 ticks → pip_value = 10 × tick_value when tick_size == point.
    fake = _FakeMT5({
        "EURUSDm": {"point": 0.00001, "trade_tick_value": 1.0, "trade_tick_size": 0.00001},
    })
    set_resolver(PipResolver(fake))
    assert pip_value("EURUSDm") == pytest.approx(10.0)


def test_resolver_falls_back_when_mt5_returns_none():
    # Symbol not in MT5's universe → resolver should fall back to the
    # hardcoded default rather than crashing or returning 0.
    fake = _FakeMT5({})
    set_resolver(PipResolver(fake))
    assert pip_size("EURUSD") == pytest.approx(0.0001)
    assert pip_value("EURUSD") == pytest.approx(10.0)


def test_resolver_caches_per_symbol():
    fake = _FakeMT5({
        "EURUSD": {"point": 0.00001, "trade_tick_value": 1.0, "trade_tick_size": 0.00001},
    })
    resolver = PipResolver(fake)
    set_resolver(resolver)
    pip_size("EURUSD")
    pip_size("EURUSD")
    pip_size("EURUSD")
    # First call selects + queries; subsequent calls hit the cache and add
    # nothing. Two select calls would be the worst case (one for size, one
    # for value), three is a regression.
    assert fake.selected.count("EURUSD") <= 2


def test_no_resolver_uses_fallback_table():
    # Sanity: without a resolver installed (mock mode, tests, local dev) the
    # behaviour matches the old hardcoded version exactly.
    set_resolver(None)
    assert pip_size("EURUSD") == 0.0001
    assert pip_size("USDJPY") == 0.01
    assert pip_size("XAUUSD") == 0.1
