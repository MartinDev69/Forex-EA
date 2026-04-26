"""Backtester — per-symbol PnL and risk-based lot sizing.

The old backtester multiplied raw price diffs by 10_000 regardless of pair,
which silently broke JPY and metals. These tests pin the corrected math.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.backtesting.engine import Trade, _pnl, run_backtest
from src.risk.position_sizing import pip_size
from src.strategies.base import Signal, SignalType, Strategy


def _flat_ohlc(n: int, price: float, step_minutes: int = 15) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq=f"{step_minutes}min", tz="UTC")
    return pd.DataFrame(
        {"open": price, "high": price, "low": price, "close": price, "volume": 100.0},
        index=idx,
    )


# ---------------------------------------------------------------- pip math


def test_pip_size_defaults_by_class():
    assert pip_size("EURUSD") == 0.0001
    assert pip_size("GBPUSD") == 0.0001
    assert pip_size("USDJPY") == 0.01
    assert pip_size("EURJPY") == 0.01
    assert pip_size("XAUUSD") == 0.1


# ---------------------------------------------------------------- PnL


def _trade(symbol: str, side: SignalType, entry: float, lot: float = 1.0) -> Trade:
    return Trade(
        entry_time=pd.Timestamp("2024-01-01", tz="UTC"),
        exit_time=None,
        side=side,
        symbol=symbol,
        entry_price=entry,
        exit_price=None,
        stop_loss=0.0,
        take_profit=0.0,
        lot_size=lot,
    )


def test_pnl_eurusd_matches_pips_times_pip_value():
    # 50 pips on EURUSD at 1 lot = $500.
    t = _trade("EURUSD", SignalType.BUY, 1.1000, lot=1.0)
    assert _pnl(t, 1.1050) == pytest.approx(500.0)


def test_pnl_usdjpy_uses_0_01_pip_size():
    # 50 pips on USDJPY = 0.50 price move. At 1 lot ≈ $9/pip → $450.
    t = _trade("USDJPY", SignalType.BUY, 150.00, lot=1.0)
    assert _pnl(t, 150.50) == pytest.approx(450.0, rel=1e-6)


def test_pnl_sell_inverts_direction():
    long = _trade("EURUSD", SignalType.BUY, 1.1000, lot=1.0)
    short = _trade("EURUSD", SignalType.SELL, 1.1000, lot=1.0)
    # Price drops by 20 pips: short wins, long loses, same magnitude.
    assert _pnl(long, 1.0980) == pytest.approx(-_pnl(short, 1.0980))


def test_pnl_scales_linearly_with_lot_size():
    full = _pnl(_trade("EURUSD", SignalType.BUY, 1.1, lot=1.0), 1.105)
    tenth = _pnl(_trade("EURUSD", SignalType.BUY, 1.1, lot=0.1), 1.105)
    assert tenth == pytest.approx(full / 10)


# ---------------------------------------------------------------- integration


class _OneShotStrategy(Strategy):
    name = "oneshot"

    def __init__(self, symbol: str, fire_at_bar: int, stop_pips: float, tp_pips: float) -> None:
        super().__init__(symbol)
        self._fire_at = fire_at_bar
        self._stop_pips = stop_pips
        self._tp_pips = tp_pips
        self._fired = False

    def generate_signal(self, ohlc):
        if self._fired or len(ohlc) - 1 < self._fire_at:
            return Signal(SignalType.HOLD, self.symbol, ohlc.index[-1], float(ohlc["close"].iloc[-1]))
        self._fired = True
        price = float(ohlc["close"].iloc[-1])
        ps = pip_size(self.symbol)
        return Signal(
            SignalType.BUY, self.symbol, ohlc.index[-1], price,
            stop_loss=price - self._stop_pips * ps,
            take_profit=price + self._tp_pips * ps,
        )


def test_run_backtest_sizes_lots_from_risk_budget():
    # 200 flat bars at 1.10; strategy fires at bar 100 with 50-pip SL and wide TP.
    # Risk 1% of $10k = $100; stop = 50 pips × $10/pip-per-lot → 0.2 lots.
    ohlc = _flat_ohlc(200, price=1.10)
    # Make the 101st bar a wick up to hit TP so the trade closes with a known move.
    ohlc.iloc[101, ohlc.columns.get_loc("high")] = 1.1100  # 100-pip wick up
    ohlc.iloc[101, ohlc.columns.get_loc("low")] = 1.10
    strat = _OneShotStrategy("EURUSD", fire_at_bar=100, stop_pips=50, tp_pips=80)

    res = run_backtest(ohlc, strat, starting_equity=10_000, risk_per_trade_pct=0.01, lookback=100)

    assert res.total_trades == 1
    t = res.trades[0]
    assert t.lot_size == pytest.approx(0.2, abs=0.01)
    # 80 pips × $10/pip × 0.2 lots = $160 profit.
    assert t.pnl == pytest.approx(160.0, rel=0.01)
    assert res.final_equity == pytest.approx(10_160.0, rel=0.01)


def test_run_backtest_symbol_override():
    ohlc = _flat_ohlc(200, price=150.00)
    ohlc.iloc[101, ohlc.columns.get_loc("high")] = 151.00
    ohlc.iloc[101, ohlc.columns.get_loc("low")] = 150.00
    # Strategy's own symbol says EURUSD; caller overrides with USDJPY so pip math shifts.
    strat = _OneShotStrategy("EURUSD", fire_at_bar=100, stop_pips=50, tp_pips=80)

    res = run_backtest(ohlc, strat, starting_equity=10_000, risk_per_trade_pct=0.01,
                       lookback=100, symbol="USDJPY")

    assert res.total_trades == 1
    assert res.trades[0].symbol == "USDJPY"
