"""Moving-average crossover strategy.

Entry:
  BUY  when fast EMA crosses above slow EMA
  SELL when fast EMA crosses below slow EMA

Stops/targets are ATR-based — actual position sizing is handled by the risk module.
"""
from __future__ import annotations

import pandas as pd

from src.indicators.trend import ema
from src.indicators.volatility import atr

from .base import Signal, SignalType, Strategy


class MACrossoverStrategy(Strategy):
    name = "ma_crossover"
    preferred_regimes = frozenset({"trend_up", "trend_down"})

    def __init__(
        self,
        symbol: str,
        fast_period: int = 12,
        slow_period: int = 26,
        atr_period: int = 14,
        atr_sl_mult: float = 1.5,
        atr_tp_mult: float = 3.0,
    ) -> None:
        super().__init__(symbol)
        if fast_period >= slow_period:
            raise ValueError("fast_period must be < slow_period")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.atr_period = atr_period
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mult = atr_tp_mult

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        if len(ohlc) < self.slow_period + 2:
            return self._hold(ohlc, "insufficient bars")

        close = ohlc["close"]
        fast = ema(close, self.fast_period)
        slow = ema(close, self.slow_period)
        atr_val = atr(ohlc["high"], ohlc["low"], close, self.atr_period)

        last_fast, prev_fast = fast.iloc[-1], fast.iloc[-2]
        last_slow, prev_slow = slow.iloc[-1], slow.iloc[-2]
        last_close = close.iloc[-1]
        last_atr = atr_val.iloc[-1]
        ts = ohlc.index[-1]

        crossed_up = prev_fast <= prev_slow and last_fast > last_slow
        crossed_down = prev_fast >= prev_slow and last_fast < last_slow

        if crossed_up:
            return Signal(
                type=SignalType.BUY,
                symbol=self.symbol,
                timestamp=ts,
                price=last_close,
                stop_loss=last_close - self.atr_sl_mult * last_atr,
                take_profit=last_close + self.atr_tp_mult * last_atr,
                reason=f"EMA{self.fast_period} crossed above EMA{self.slow_period}",
            )
        if crossed_down:
            return Signal(
                type=SignalType.SELL,
                symbol=self.symbol,
                timestamp=ts,
                price=last_close,
                stop_loss=last_close + self.atr_sl_mult * last_atr,
                take_profit=last_close - self.atr_tp_mult * last_atr,
                reason=f"EMA{self.fast_period} crossed below EMA{self.slow_period}",
            )

        return self._hold(ohlc, "no crossover")

    def _hold(self, ohlc: pd.DataFrame, reason: str) -> Signal:
        return Signal(
            type=SignalType.HOLD,
            symbol=self.symbol,
            timestamp=ohlc.index[-1] if len(ohlc) else pd.Timestamp.now(),
            price=float(ohlc["close"].iloc[-1]) if len(ohlc) else 0.0,
            reason=reason,
        )
