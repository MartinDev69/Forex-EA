"""Feature engineering for the signal meta-model.

Takes the OHLC window visible at the moment a strategy fires and the candidate
Signal, and produces a fixed-shape numeric vector. The meta-model uses this to
decide whether the trade is likely to hit TP before SL.

Invariants:
  * Feature order is stable — `FEATURE_NAMES` is the source of truth and must
    match what the model was trained on. If you add a feature, retrain.
  * All features are computed from bars up to and including the signal bar.
    Don't leak future data.
"""
from __future__ import annotations

import math

import pandas as pd

from src.indicators.momentum import rsi, stochastic
from src.indicators.trend import ema, macd
from src.indicators.volatility import atr, bollinger_bands
from src.strategies.base import Signal, SignalType

# Strategies the meta-model has seen during training. Unknown names get all
# zeros across the one-hot block — safer than raising at inference time.
_KNOWN_STRATEGIES = ("ma_crossover", "rsi_mean_reversion", "donchian_breakout")

FEATURE_NAMES: tuple[str, ...] = (
    "side_is_buy",
    "rsi_14",
    "macd_hist",
    "atr_ratio",       # ATR / close — volatility normalized by price
    "bb_pct_b",        # where price sits inside its Bollinger band, 0..1ish
    "ret_1",
    "ret_5",
    "ret_20",
    "stoch_k",
    "stoch_d",
    "ema_fast_over_slow",
    "hour_sin",
    "hour_cos",
    "dow",
    *(f"strategy_{name}" for name in _KNOWN_STRATEGIES),
)

# Minimum bars required so every indicator has a value at the last row.
# 20 = slowest default window (BB, ret_20, macd slow≈26 needs 26+).
MIN_BARS = 30


def build_feature_vector(signal: Signal, ohlc: pd.DataFrame, strategy_name: str) -> pd.Series:
    """Extract features aligned to the last bar in `ohlc`.

    Returns a pd.Series indexed by FEATURE_NAMES. NaN-safe: if any indicator
    can't be computed (not enough bars), we fill with 0.0 — the model sees
    these as neutral. Caller can check `has_enough_bars(ohlc)` first.
    """
    close = ohlc["close"]
    last_close = float(close.iloc[-1])

    # Indicators
    rsi_series = rsi(close, 14)
    macd_df = macd(close)
    atr_series = atr(ohlc["high"], ohlc["low"], close, 14)
    bb = bollinger_bands(close, 20, 2.0)
    stoch = stochastic(ohlc["high"], ohlc["low"], close, 14, 3)
    ema_fast = ema(close, 12)
    ema_slow = ema(close, 26)

    def last_or_zero(s: pd.Series) -> float:
        v = s.iloc[-1]
        return 0.0 if pd.isna(v) else float(v)

    bb_upper = last_or_zero(bb["upper"])
    bb_lower = last_or_zero(bb["lower"])
    bb_range = bb_upper - bb_lower
    bb_pct_b = (last_close - bb_lower) / bb_range if bb_range > 0 else 0.5

    atr_val = last_or_zero(atr_series)
    atr_ratio = atr_val / last_close if last_close else 0.0

    ema_s_val = last_or_zero(ema_slow)
    ema_fast_over_slow = (last_or_zero(ema_fast) / ema_s_val) if ema_s_val else 1.0

    # Returns over 1, 5, 20 bars (log-returns, safer than pct change for tails)
    def log_return(n: int) -> float:
        if len(close) <= n:
            return 0.0
        prev = float(close.iloc[-1 - n])
        if prev <= 0 or last_close <= 0:
            return 0.0
        return math.log(last_close / prev)

    ts = ohlc.index[-1]
    # Works for pandas Timestamp and for naive datetimes coerced by the feed.
    ts = pd.Timestamp(ts)
    hour = ts.hour
    dow = ts.dayofweek

    side_is_buy = 1.0 if signal.type == SignalType.BUY else 0.0

    strategy_onehot = {
        f"strategy_{name}": (1.0 if name == strategy_name else 0.0)
        for name in _KNOWN_STRATEGIES
    }

    values = {
        "side_is_buy": side_is_buy,
        "rsi_14": last_or_zero(rsi_series),
        "macd_hist": last_or_zero(macd_df["histogram"]),
        "atr_ratio": atr_ratio,
        "bb_pct_b": bb_pct_b,
        "ret_1": log_return(1),
        "ret_5": log_return(5),
        "ret_20": log_return(20),
        "stoch_k": last_or_zero(stoch["%K"]),
        "stoch_d": last_or_zero(stoch["%D"]),
        "ema_fast_over_slow": ema_fast_over_slow,
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "dow": float(dow),
        **strategy_onehot,
    }

    return pd.Series(values, index=list(FEATURE_NAMES), dtype="float64")


def has_enough_bars(ohlc: pd.DataFrame) -> bool:
    return len(ohlc) >= MIN_BARS
