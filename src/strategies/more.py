"""Nine additional strategies — kept in one file because each one is
small (~30-60 lines) and the per-strategy boilerplate is identical.

All of them populate ``signal.indicators`` so the dashboard can show
"what the strategy saw" when the trade was opened. Stops/targets use
ATR multiples to keep risk consistent across symbols.
"""
from __future__ import annotations

import pandas as pd

from src.indicators.directional import adx
from src.indicators.momentum import rsi, stochastic
from src.indicators.trend import ema, macd
from src.indicators.volatility import atr, bollinger_bands

from .base import Signal, SignalType, Strategy


def _hold(symbol: str, ohlc: pd.DataFrame, reason: str,
          indicators: dict | None = None) -> Signal:
    return Signal(
        type=SignalType.HOLD,
        symbol=symbol,
        timestamp=ohlc.index[-1] if len(ohlc) else pd.Timestamp.now(),
        price=float(ohlc["close"].iloc[-1]) if len(ohlc) else 0.0,
        reason=reason,
        indicators=indicators or {},
    )


# --------------------------------------------------------------- 1. MACD cross
class MACDCrossStrategy(Strategy):
    """MACD line crosses signal line. Histogram polarity confirms momentum.

    Trend setup. Adds an EMA200 filter so we only buy crosses above the
    long-term trend (and sell below) — keeps it from chopping in ranges.
    """
    name = "macd_cross"
    preferred_regimes = frozenset({"trend_up", "trend_down"})

    def __init__(self, symbol: str, fast: int = 12, slow: int = 26,
                 signal_period: int = 9, atr_period: int = 14,
                 atr_sl_mult: float = 1.5, atr_tp_mult: float = 3.0) -> None:
        super().__init__(symbol)
        self.fast, self.slow, self.signal_period = fast, slow, signal_period
        self.atr_period = atr_period
        self.atr_sl_mult, self.atr_tp_mult = atr_sl_mult, atr_tp_mult

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        need = max(self.slow + self.signal_period, 200) + 2
        if len(ohlc) < need:
            return _hold(self.symbol, ohlc, "insufficient bars")
        close = ohlc["close"]
        m = macd(close, self.fast, self.slow, self.signal_period)
        ema200 = ema(close, 200).iloc[-1]
        last_atr = atr(ohlc["high"], ohlc["low"], close, self.atr_period).iloc[-1]
        macd_now, macd_prev = m["macd"].iloc[-1], m["macd"].iloc[-2]
        sig_now, sig_prev = m["signal"].iloc[-1], m["signal"].iloc[-2]
        last_close = float(close.iloc[-1])
        ts = ohlc.index[-1]
        ind = {
            "macd": float(macd_now), "signal": float(sig_now),
            "histogram": float(m["histogram"].iloc[-1]),
            "ema200": float(ema200),
        }
        crossed_up = macd_prev <= sig_prev and macd_now > sig_now
        crossed_down = macd_prev >= sig_prev and macd_now < sig_now
        if crossed_up and last_close > ema200:
            return Signal(SignalType.BUY, self.symbol, ts, last_close,
                stop_loss=last_close - self.atr_sl_mult * last_atr,
                take_profit=last_close + self.atr_tp_mult * last_atr,
                reason="MACD crossed above signal line above EMA200",
                indicators=ind)
        if crossed_down and last_close < ema200:
            return Signal(SignalType.SELL, self.symbol, ts, last_close,
                stop_loss=last_close + self.atr_sl_mult * last_atr,
                take_profit=last_close - self.atr_tp_mult * last_atr,
                reason="MACD crossed below signal line below EMA200",
                indicators=ind)
        return _hold(self.symbol, ohlc, "no MACD cross with trend", ind)


# --------------------------------------------------------------- 2. BB bounce
class BollingerBounceStrategy(Strategy):
    """Mean-reversion: tag the lower BB with RSI < 35 → BUY; tag the upper
    with RSI > 65 → SELL. Range setup; gated to range regimes only.
    """
    name = "bollinger_bounce"
    preferred_regimes = frozenset({"range"})

    def __init__(self, symbol: str, period: int = 20, num_std: float = 2.0,
                 rsi_period: int = 14, rsi_low: float = 35, rsi_high: float = 65,
                 atr_period: int = 14, atr_sl_mult: float = 1.0, atr_tp_mult: float = 1.5) -> None:
        super().__init__(symbol)
        self.period, self.num_std = period, num_std
        self.rsi_period, self.rsi_low, self.rsi_high = rsi_period, rsi_low, rsi_high
        self.atr_period = atr_period
        self.atr_sl_mult, self.atr_tp_mult = atr_sl_mult, atr_tp_mult

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        if len(ohlc) < self.period + 2:
            return _hold(self.symbol, ohlc, "insufficient bars")
        bb = bollinger_bands(ohlc["close"], self.period, self.num_std)
        rsi_val = rsi(ohlc["close"], self.rsi_period).iloc[-1]
        last_atr = atr(ohlc["high"], ohlc["low"], ohlc["close"], self.atr_period).iloc[-1]
        last = ohlc.iloc[-1]
        close = float(last["close"]); low = float(last["low"]); high = float(last["high"])
        upper, middle, lower = bb["upper"].iloc[-1], bb["middle"].iloc[-1], bb["lower"].iloc[-1]
        ts = ohlc.index[-1]
        ind = {"bb_upper": float(upper), "bb_middle": float(middle),
               "bb_lower": float(lower), "rsi": float(rsi_val)}
        if low <= lower and rsi_val < self.rsi_low:
            return Signal(SignalType.BUY, self.symbol, ts, close,
                stop_loss=close - self.atr_sl_mult * last_atr,
                take_profit=float(middle),
                reason=f"Tagged lower BB ({lower:.5f}), RSI {rsi_val:.0f}",
                indicators=ind)
        if high >= upper and rsi_val > self.rsi_high:
            return Signal(SignalType.SELL, self.symbol, ts, close,
                stop_loss=close + self.atr_sl_mult * last_atr,
                take_profit=float(middle),
                reason=f"Tagged upper BB ({upper:.5f}), RSI {rsi_val:.0f}",
                indicators=ind)
        return _hold(self.symbol, ohlc, "no BB bounce setup", ind)


# --------------------------------------------------------------- 3. BB squeeze
class BollingerSqueezeStrategy(Strategy):
    """Volatility expansion. After BB width contracts to a multi-bar low,
    take the breakout in whichever direction price exits the band.
    """
    name = "bollinger_squeeze"
    preferred_regimes = frozenset({"trend_up", "trend_down", "range"})

    def __init__(self, symbol: str, period: int = 20, num_std: float = 2.0,
                 squeeze_lookback: int = 30, atr_period: int = 14,
                 atr_sl_mult: float = 1.5, atr_tp_mult: float = 3.0) -> None:
        super().__init__(symbol)
        self.period, self.num_std = period, num_std
        self.squeeze_lookback = squeeze_lookback
        self.atr_period = atr_period
        self.atr_sl_mult, self.atr_tp_mult = atr_sl_mult, atr_tp_mult

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        need = self.period + self.squeeze_lookback + 2
        if len(ohlc) < need:
            return _hold(self.symbol, ohlc, "insufficient bars")
        bb = bollinger_bands(ohlc["close"], self.period, self.num_std)
        width = (bb["upper"] - bb["lower"])
        last_atr = atr(ohlc["high"], ohlc["low"], ohlc["close"], self.atr_period).iloc[-1]
        last = ohlc.iloc[-1]
        close = float(last["close"]); ts = ohlc.index[-1]
        upper, lower = float(bb["upper"].iloc[-1]), float(bb["lower"].iloc[-1])
        recent_min_width = float(width.iloc[-self.squeeze_lookback - 1:-1].min())
        was_squeezed = float(width.iloc[-2]) <= recent_min_width * 1.05
        ind = {"bb_upper": upper, "bb_lower": lower,
               "bb_width": float(width.iloc[-1]),
               "squeeze_min_width": recent_min_width,
               "was_squeezed": int(was_squeezed)}
        if not was_squeezed:
            return _hold(self.symbol, ohlc, "no prior squeeze", ind)
        if close > upper:
            return Signal(SignalType.BUY, self.symbol, ts, close,
                stop_loss=close - self.atr_sl_mult * last_atr,
                take_profit=close + self.atr_tp_mult * last_atr,
                reason=f"Squeeze breakout above {upper:.5f}",
                indicators=ind)
        if close < lower:
            return Signal(SignalType.SELL, self.symbol, ts, close,
                stop_loss=close + self.atr_sl_mult * last_atr,
                take_profit=close - self.atr_tp_mult * last_atr,
                reason=f"Squeeze breakout below {lower:.5f}",
                indicators=ind)
        return _hold(self.symbol, ohlc, "squeezed but no break yet", ind)


# --------------------------------------------------------------- 4. Stoch reversal
class StochasticReversalStrategy(Strategy):
    """Stochastic %K crosses %D out of OB/OS zones. Range-mean-reversion."""
    name = "stochastic_reversal"
    preferred_regimes = frozenset({"range"})

    def __init__(self, symbol: str, k_period: int = 14, d_period: int = 3,
                 oversold: float = 20, overbought: float = 80,
                 atr_period: int = 14, atr_sl_mult: float = 1.0, atr_tp_mult: float = 2.0) -> None:
        super().__init__(symbol)
        self.k_period, self.d_period = k_period, d_period
        self.oversold, self.overbought = oversold, overbought
        self.atr_period = atr_period
        self.atr_sl_mult, self.atr_tp_mult = atr_sl_mult, atr_tp_mult

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        if len(ohlc) < self.k_period + self.d_period + 2:
            return _hold(self.symbol, ohlc, "insufficient bars")
        st = stochastic(ohlc["high"], ohlc["low"], ohlc["close"],
                        self.k_period, self.d_period)
        k_now, k_prev = st["%K"].iloc[-1], st["%K"].iloc[-2]
        d_now, d_prev = st["%D"].iloc[-1], st["%D"].iloc[-2]
        last_atr = atr(ohlc["high"], ohlc["low"], ohlc["close"], self.atr_period).iloc[-1]
        close = float(ohlc["close"].iloc[-1]); ts = ohlc.index[-1]
        ind = {"stoch_k": float(k_now), "stoch_d": float(d_now)}
        crossed_up = k_prev <= d_prev and k_now > d_now
        crossed_down = k_prev >= d_prev and k_now < d_now
        if crossed_up and k_prev < self.oversold:
            return Signal(SignalType.BUY, self.symbol, ts, close,
                stop_loss=close - self.atr_sl_mult * last_atr,
                take_profit=close + self.atr_tp_mult * last_atr,
                reason=f"Stoch %K crossed above %D from oversold ({k_prev:.0f})",
                indicators=ind)
        if crossed_down and k_prev > self.overbought:
            return Signal(SignalType.SELL, self.symbol, ts, close,
                stop_loss=close + self.atr_sl_mult * last_atr,
                take_profit=close - self.atr_tp_mult * last_atr,
                reason=f"Stoch %K crossed below %D from overbought ({k_prev:.0f})",
                indicators=ind)
        return _hold(self.symbol, ohlc, "no stoch reversal", ind)


# --------------------------------------------------------------- 5. Triple MA
class TripleMAStrategy(Strategy):
    """Three EMAs (8 / 21 / 55) stacked in trend direction. Strong trend
    confirmation; only fires when all three line up the same way and
    price is on the right side of all of them.
    """
    name = "triple_ma_alignment"
    preferred_regimes = frozenset({"trend_up", "trend_down"})

    def __init__(self, symbol: str, fast: int = 8, mid: int = 21, slow: int = 55,
                 atr_period: int = 14, atr_sl_mult: float = 1.5, atr_tp_mult: float = 3.0) -> None:
        super().__init__(symbol)
        self.fast, self.mid, self.slow = fast, mid, slow
        self.atr_period = atr_period
        self.atr_sl_mult, self.atr_tp_mult = atr_sl_mult, atr_tp_mult

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        if len(ohlc) < self.slow + 2:
            return _hold(self.symbol, ohlc, "insufficient bars")
        close = ohlc["close"]
        f = ema(close, self.fast); m = ema(close, self.mid); s = ema(close, self.slow)
        last_atr = atr(ohlc["high"], ohlc["low"], close, self.atr_period).iloc[-1]
        c = float(close.iloc[-1]); ts = ohlc.index[-1]
        f_now, m_now, s_now = float(f.iloc[-1]), float(m.iloc[-1]), float(s.iloc[-1])
        f_prev = float(f.iloc[-2])
        ind = {"ema_fast": f_now, "ema_mid": m_now, "ema_slow": s_now}
        bullish_now = f_now > m_now > s_now and c > f_now
        bullish_prev = f_prev > float(m.iloc[-2])
        bearish_now = f_now < m_now < s_now and c < f_now
        bearish_prev = f_prev < float(m.iloc[-2])
        # Only fire on the bar that newly aligned, not every bar in trend.
        if bullish_now and not bullish_prev:
            return Signal(SignalType.BUY, self.symbol, ts, c,
                stop_loss=c - self.atr_sl_mult * last_atr,
                take_profit=c + self.atr_tp_mult * last_atr,
                reason="EMA8 > EMA21 > EMA55 stacked bullish",
                indicators=ind)
        if bearish_now and not bearish_prev:
            return Signal(SignalType.SELL, self.symbol, ts, c,
                stop_loss=c + self.atr_sl_mult * last_atr,
                take_profit=c - self.atr_tp_mult * last_atr,
                reason="EMA8 < EMA21 < EMA55 stacked bearish",
                indicators=ind)
        return _hold(self.symbol, ohlc, "EMAs not freshly aligned", ind)


# --------------------------------------------------------------- 6. Inside bar
class InsideBarBreakoutStrategy(Strategy):
    """Inside bar = a bar whose high/low is contained inside the prior bar.
    Compression of range → breakout of the *prior* bar's range in either
    direction is the entry. Pure price action.
    """
    name = "inside_bar_breakout"
    preferred_regimes = frozenset({"trend_up", "trend_down", "range"})

    def __init__(self, symbol: str, atr_period: int = 14,
                 atr_sl_mult: float = 1.0, atr_tp_mult: float = 2.0) -> None:
        super().__init__(symbol)
        self.atr_period = atr_period
        self.atr_sl_mult, self.atr_tp_mult = atr_sl_mult, atr_tp_mult

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        if len(ohlc) < self.atr_period + 3:
            return _hold(self.symbol, ohlc, "insufficient bars")
        # Need: bar -2 (mother), bar -1 (inside bar), bar 0 (potential breakout)
        mother = ohlc.iloc[-3]
        inside = ohlc.iloc[-2]
        cur = ohlc.iloc[-1]
        is_inside = inside["high"] < mother["high"] and inside["low"] > mother["low"]
        last_atr = atr(ohlc["high"], ohlc["low"], ohlc["close"], self.atr_period).iloc[-1]
        c = float(cur["close"]); ts = ohlc.index[-1]
        mh = float(mother["high"]); ml = float(mother["low"])
        ind = {"mother_high": mh, "mother_low": ml,
               "inside_bar": int(is_inside)}
        if not is_inside:
            return _hold(self.symbol, ohlc, "no inside bar pattern", ind)
        if cur["high"] > mother["high"]:
            return Signal(SignalType.BUY, self.symbol, ts, c,
                stop_loss=ml - 0.1 * last_atr,
                take_profit=c + self.atr_tp_mult * last_atr,
                reason=f"Inside-bar breakout above {mh:.5f}",
                indicators=ind)
        if cur["low"] < mother["low"]:
            return Signal(SignalType.SELL, self.symbol, ts, c,
                stop_loss=mh + 0.1 * last_atr,
                take_profit=c - self.atr_tp_mult * last_atr,
                reason=f"Inside-bar breakdown below {ml:.5f}",
                indicators=ind)
        return _hold(self.symbol, ohlc, "inside bar still compressing", ind)


# --------------------------------------------------------------- 7. Engulfing
class EngulfingPatternStrategy(Strategy):
    """Bullish/bearish engulfing — current candle's body fully covers the
    prior candle's body in the opposite direction. A reliable reversal
    signal at swing extremes.
    """
    name = "engulfing_pattern"
    preferred_regimes = frozenset({"range", "trend_up", "trend_down"})

    def __init__(self, symbol: str, ema_period: int = 50,
                 atr_period: int = 14, atr_sl_mult: float = 1.0, atr_tp_mult: float = 2.0) -> None:
        super().__init__(symbol)
        self.ema_period = ema_period
        self.atr_period = atr_period
        self.atr_sl_mult, self.atr_tp_mult = atr_sl_mult, atr_tp_mult

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        if len(ohlc) < max(self.ema_period, self.atr_period) + 2:
            return _hold(self.symbol, ohlc, "insufficient bars")
        prev = ohlc.iloc[-2]; cur = ohlc.iloc[-1]
        last_atr = atr(ohlc["high"], ohlc["low"], ohlc["close"], self.atr_period).iloc[-1]
        ema_val = float(ema(ohlc["close"], self.ema_period).iloc[-1])
        c = float(cur["close"]); ts = ohlc.index[-1]
        prev_body = abs(prev["close"] - prev["open"])
        cur_body = abs(cur["close"] - cur["open"])
        ind = {"ema": ema_val, "prev_body": float(prev_body),
               "cur_body": float(cur_body)}
        prev_bear = prev["close"] < prev["open"]
        cur_bull = cur["close"] > cur["open"]
        bullish_engulf = (
            prev_bear and cur_bull
            and cur["open"] <= prev["close"]
            and cur["close"] >= prev["open"]
            and cur_body > prev_body
        )
        prev_bull = prev["close"] > prev["open"]
        cur_bear = cur["close"] < cur["open"]
        bearish_engulf = (
            prev_bull and cur_bear
            and cur["open"] >= prev["close"]
            and cur["close"] <= prev["open"]
            and cur_body > prev_body
        )
        if bullish_engulf and c < ema_val:  # at a low
            return Signal(SignalType.BUY, self.symbol, ts, c,
                stop_loss=float(cur["low"]) - 0.1 * last_atr,
                take_profit=c + self.atr_tp_mult * last_atr,
                reason="Bullish engulfing below EMA50",
                indicators=ind)
        if bearish_engulf and c > ema_val:  # at a high
            return Signal(SignalType.SELL, self.symbol, ts, c,
                stop_loss=float(cur["high"]) + 0.1 * last_atr,
                take_profit=c - self.atr_tp_mult * last_atr,
                reason="Bearish engulfing above EMA50",
                indicators=ind)
        return _hold(self.symbol, ohlc, "no engulfing pattern", ind)


# --------------------------------------------------------------- 8. EMA pullback
class EMAPullbackStrategy(Strategy):
    """Pull-back to EMA21 in an established trend (defined by EMA50 slope
    + price relative to EMA200). Buy the dip in an uptrend, sell the rip
    in a downtrend. One of the more reliable trend-continuation setups.
    """
    name = "ema_pullback"
    preferred_regimes = frozenset({"trend_up", "trend_down"})

    def __init__(self, symbol: str, fast: int = 21, trend_filter: int = 200,
                 touch_atr: float = 0.4, atr_period: int = 14,
                 atr_sl_mult: float = 1.5, atr_tp_mult: float = 3.0) -> None:
        super().__init__(symbol)
        self.fast = fast; self.trend_filter = trend_filter
        self.touch_atr = touch_atr
        self.atr_period = atr_period
        self.atr_sl_mult, self.atr_tp_mult = atr_sl_mult, atr_tp_mult

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        if len(ohlc) < self.trend_filter + 2:
            return _hold(self.symbol, ohlc, "insufficient bars")
        close = ohlc["close"]
        e_fast = float(ema(close, self.fast).iloc[-1])
        e_trend = float(ema(close, self.trend_filter).iloc[-1])
        e_trend_prev = float(ema(close, self.trend_filter).iloc[-5])
        last_atr = atr(ohlc["high"], ohlc["low"], close, self.atr_period).iloc[-1]
        c = float(close.iloc[-1]); ts = ohlc.index[-1]
        ind = {"ema_fast": e_fast, "ema_trend": e_trend,
               "trend_slope": e_trend - e_trend_prev}
        uptrend = c > e_trend and e_trend > e_trend_prev
        downtrend = c < e_trend and e_trend < e_trend_prev
        # Touch defined as low (or high) within touch_atr * atr of the fast EMA
        last = ohlc.iloc[-1]
        touched_above = abs(float(last["low"]) - e_fast) <= self.touch_atr * last_atr
        touched_below = abs(float(last["high"]) - e_fast) <= self.touch_atr * last_atr
        if uptrend and touched_above and c > e_fast:
            return Signal(SignalType.BUY, self.symbol, ts, c,
                stop_loss=c - self.atr_sl_mult * last_atr,
                take_profit=c + self.atr_tp_mult * last_atr,
                reason=f"Pullback to EMA{self.fast} in uptrend",
                indicators=ind)
        if downtrend and touched_below and c < e_fast:
            return Signal(SignalType.SELL, self.symbol, ts, c,
                stop_loss=c + self.atr_sl_mult * last_atr,
                take_profit=c - self.atr_tp_mult * last_atr,
                reason=f"Pullback to EMA{self.fast} in downtrend",
                indicators=ind)
        return _hold(self.symbol, ohlc, "no pullback in trend", ind)


# --------------------------------------------------------------- 9. ADX breakout
class ADXBreakoutStrategy(Strategy):
    """Take a breakout only when ADX confirms a real trend (>= 25 and
    rising). Filters out the chop that destroys raw breakout systems.
    """
    name = "adx_breakout"
    preferred_regimes = frozenset({"trend_up", "trend_down"})

    def __init__(self, symbol: str, adx_period: int = 14, adx_threshold: float = 25,
                 lookback: int = 20, atr_period: int = 14,
                 atr_sl_mult: float = 2.0, atr_tp_mult: float = 4.0) -> None:
        super().__init__(symbol)
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.lookback = lookback
        self.atr_period = atr_period
        self.atr_sl_mult, self.atr_tp_mult = atr_sl_mult, atr_tp_mult

    def generate_signal(self, ohlc: pd.DataFrame) -> Signal:
        need = max(self.adx_period * 2, self.lookback) + 2
        if len(ohlc) < need:
            return _hold(self.symbol, ohlc, "insufficient bars")
        adx_df = adx(ohlc["high"], ohlc["low"], ohlc["close"], self.adx_period)
        adx_now = float(adx_df["adx"].iloc[-1])
        adx_prev = float(adx_df["adx"].iloc[-3])
        plus_di = float(adx_df["plus_di"].iloc[-1])
        minus_di = float(adx_df["minus_di"].iloc[-1])
        prior = ohlc.iloc[:-1]
        upper = float(prior["high"].rolling(self.lookback).max().iloc[-1])
        lower = float(prior["low"].rolling(self.lookback).min().iloc[-1])
        last = ohlc.iloc[-1]
        c = float(last["close"]); ts = ohlc.index[-1]
        last_atr = atr(ohlc["high"], ohlc["low"], ohlc["close"], self.atr_period).iloc[-1]
        ind = {"adx": adx_now, "plus_di": plus_di, "minus_di": minus_di,
               "lookback_high": upper, "lookback_low": lower}
        adx_strong = adx_now >= self.adx_threshold and adx_now > adx_prev
        if not adx_strong:
            return _hold(self.symbol, ohlc, f"ADX {adx_now:.0f} not strong enough", ind)
        if float(last["high"]) > upper and plus_di > minus_di:
            return Signal(SignalType.BUY, self.symbol, ts, c,
                stop_loss=c - self.atr_sl_mult * last_atr,
                take_profit=c + self.atr_tp_mult * last_atr,
                reason=f"ADX {adx_now:.0f} confirmed breakout above {upper:.5f}",
                indicators=ind)
        if float(last["low"]) < lower and minus_di > plus_di:
            return Signal(SignalType.SELL, self.symbol, ts, c,
                stop_loss=c + self.atr_sl_mult * last_atr,
                take_profit=c - self.atr_tp_mult * last_atr,
                reason=f"ADX {adx_now:.0f} confirmed breakdown below {lower:.5f}",
                indicators=ind)
        return _hold(self.symbol, ohlc, "ADX strong but no break", ind)


__all__ = [
    "MACDCrossStrategy",
    "BollingerBounceStrategy",
    "BollingerSqueezeStrategy",
    "StochasticReversalStrategy",
    "TripleMAStrategy",
    "InsideBarBreakoutStrategy",
    "EngulfingPatternStrategy",
    "EMAPullbackStrategy",
    "ADXBreakoutStrategy",
]
