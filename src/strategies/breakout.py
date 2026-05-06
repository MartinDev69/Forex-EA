"""Donchian channel breakout strategy.

Classic trend-following setup:
  BUY  when close breaks above the highest high of the last N bars
  SELL when close breaks below the lowest low of the last N bars

Exit: ATR-based stop; target is the opposite channel boundary or ATR multiple.
"""
from __future__ import annotations

import pandas as pd

from src.indicators.volatility import atr

from .base import Signal, SignalType, Strategy


class DonchianBreakoutStrategy(Strategy):
    name = "donchian_breakout"
    # Breakouts need either a trend already forming or a volatility expansion.
    # We gate by trend here; volatility is what the breakout itself proves.
    preferred_regimes = frozenset({"trend_up", "trend_down", "range"})

    def __init__(
        self,
        symbol: str,
        channel_period: int = 20,
        atr_period: int = 14,
        atr_sl_mult: float = 2.0,
        atr_tp_mult: float = 4.0,
    ) -> None:
        super().__init__(symbol)
        if channel_period < 2:
            raise ValueError("channel_period must be >= 2")
        self.channel_period = channel_period
        self.atr_period = atr_period
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mult = atr_tp_mult

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        if len(ohlc) < self.channel_period + self.atr_period + 2:
            return self._hold(ohlc, "insufficient bars")

        # Channel is built from bars BEFORE the current one — avoid look-ahead.
        prior = ohlc.iloc[:-1]
        upper = prior["high"].rolling(self.channel_period).max().iloc[-1]
        lower = prior["low"].rolling(self.channel_period).min().iloc[-1]

        last = ohlc.iloc[-1]
        close = float(last["close"])
        high = float(last["high"])
        low = float(last["low"])
        ts = ohlc.index[-1]
        last_atr = atr(ohlc["high"], ohlc["low"], ohlc["close"], self.atr_period).iloc[-1]

        ind = {"channel_high": float(upper), "channel_low": float(lower),
               "atr": float(last_atr)}
        if high > upper:
            return Signal(
                type=SignalType.BUY,
                symbol=self.symbol,
                timestamp=ts,
                price=close,
                stop_loss=close - self.atr_sl_mult * last_atr,
                take_profit=close + self.atr_tp_mult * last_atr,
                reason=f"Close broke above {self.channel_period}-bar high ({upper:.5f})",
                indicators=ind,
            )
        if low < lower:
            return Signal(
                type=SignalType.SELL,
                symbol=self.symbol,
                timestamp=ts,
                price=close,
                stop_loss=close + self.atr_sl_mult * last_atr,
                take_profit=close - self.atr_tp_mult * last_atr,
                reason=f"Close broke below {self.channel_period}-bar low ({lower:.5f})",
                indicators=ind,
            )

        return self._hold(ohlc, f"inside channel [{lower:.5f}, {upper:.5f}]", ind)

    def _hold(self, ohlc: pd.DataFrame, reason: str, indicators: dict | None = None) -> Signal:
        return Signal(
            type=SignalType.HOLD,
            symbol=self.symbol,
            timestamp=ohlc.index[-1] if len(ohlc) else pd.Timestamp.now(),
            price=float(ohlc["close"].iloc[-1]) if len(ohlc) else 0.0,
            reason=reason,
            indicators=indicators or {},
        )
