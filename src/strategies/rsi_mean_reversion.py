"""RSI mean-reversion strategy.

Entry:
  BUY  when RSI crosses up through `oversold` (typical: 30)
  SELL when RSI crosses down through `overbought` (typical: 70)

Exit: ATR-based stop and take-profit. A tight ATR multiple is appropriate
here — mean reversion trades should close fast.
"""
from __future__ import annotations

import pandas as pd

from src.indicators.momentum import rsi
from src.indicators.volatility import atr

from .base import Signal, SignalType, Strategy


class RSIMeanReversionStrategy(Strategy):
    name = "rsi_mean_reversion"
    # Mean reversion only works when price actually reverts — in strong trends
    # the "overbought" condition can persist for dozens of bars.
    preferred_regimes = frozenset({"range"})

    def __init__(
        self,
        symbol: str,
        rsi_period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
        atr_period: int = 14,
        atr_sl_mult: float = 1.5,
        atr_tp_mult: float = 2.0,
    ) -> None:
        super().__init__(symbol)
        if not 0 < oversold < overbought < 100:
            raise ValueError("oversold must be < overbought, both in (0, 100)")
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought
        self.atr_period = atr_period
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mult = atr_tp_mult

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        min_bars = max(self.rsi_period, self.atr_period) + 2
        if len(ohlc) < min_bars:
            return self._hold(ohlc, "insufficient bars")

        close = ohlc["close"]
        rsi_series = rsi(close, self.rsi_period)
        atr_val = atr(ohlc["high"], ohlc["low"], close, self.atr_period)

        last_rsi, prev_rsi = rsi_series.iloc[-1], rsi_series.iloc[-2]
        last_close = close.iloc[-1]
        last_atr = atr_val.iloc[-1]
        ts = ohlc.index[-1]

        crossed_up_from_oversold = prev_rsi <= self.oversold < last_rsi
        crossed_down_from_overbought = prev_rsi >= self.overbought > last_rsi

        if crossed_up_from_oversold:
            return Signal(
                type=SignalType.BUY,
                symbol=self.symbol,
                timestamp=ts,
                price=last_close,
                stop_loss=last_close - self.atr_sl_mult * last_atr,
                take_profit=last_close + self.atr_tp_mult * last_atr,
                reason=f"RSI crossed up through {self.oversold} ({prev_rsi:.1f}→{last_rsi:.1f})",
            )
        if crossed_down_from_overbought:
            return Signal(
                type=SignalType.SELL,
                symbol=self.symbol,
                timestamp=ts,
                price=last_close,
                stop_loss=last_close + self.atr_sl_mult * last_atr,
                take_profit=last_close - self.atr_tp_mult * last_atr,
                reason=f"RSI crossed down through {self.overbought} ({prev_rsi:.1f}→{last_rsi:.1f})",
            )

        return self._hold(ohlc, f"RSI at {last_rsi:.1f}")

    def _hold(self, ohlc: pd.DataFrame, reason: str) -> Signal:
        return Signal(
            type=SignalType.HOLD,
            symbol=self.symbol,
            timestamp=ohlc.index[-1] if len(ohlc) else pd.Timestamp.now(),
            price=float(ohlc["close"].iloc[-1]) if len(ohlc) else 0.0,
            reason=reason,
        )
