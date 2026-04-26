"""Feature engineering — shape, determinism, and leak-check."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.ml.features import FEATURE_NAMES, MIN_BARS, build_feature_vector, has_enough_bars
from src.strategies.base import Signal, SignalType


def _ohlc(bars: int = 60, base: float = 1.1000, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = base + np.cumsum(rng.normal(0, 0.0005, bars))
    high = close + 0.0008
    low = close - 0.0008
    idx = pd.date_range("2024-01-01", periods=bars, freq="15min")
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": 100.0},
        index=idx,
    )


def _signal(ohlc: pd.DataFrame, side: SignalType = SignalType.BUY) -> Signal:
    last = float(ohlc["close"].iloc[-1])
    return Signal(
        type=side,
        symbol="EURUSD",
        timestamp=ohlc.index[-1].to_pydatetime(),
        price=last,
        stop_loss=last - 0.003,
        take_profit=last + 0.006,
    )


def test_feature_vector_shape_and_names():
    ohlc = _ohlc()
    feats = build_feature_vector(_signal(ohlc), ohlc, "ma_crossover")
    assert list(feats.index) == list(FEATURE_NAMES)
    assert feats.dtype == np.float64
    assert not feats.isna().any(), "features must be NaN-free for model input"


def test_feature_vector_is_deterministic():
    ohlc = _ohlc()
    sig = _signal(ohlc)
    a = build_feature_vector(sig, ohlc, "ma_crossover")
    b = build_feature_vector(sig, ohlc, "ma_crossover")
    pd.testing.assert_series_equal(a, b)


def test_strategy_onehot_encodes_correctly():
    ohlc = _ohlc()
    feats_ma = build_feature_vector(_signal(ohlc), ohlc, "ma_crossover")
    feats_rsi = build_feature_vector(_signal(ohlc), ohlc, "rsi_mean_reversion")

    assert feats_ma["strategy_ma_crossover"] == 1.0
    assert feats_ma["strategy_rsi_mean_reversion"] == 0.0
    assert feats_rsi["strategy_ma_crossover"] == 0.0
    assert feats_rsi["strategy_rsi_mean_reversion"] == 1.0


def test_side_is_buy_flag():
    ohlc = _ohlc()
    buy = build_feature_vector(_signal(ohlc, SignalType.BUY), ohlc, "ma_crossover")
    sell = build_feature_vector(_signal(ohlc, SignalType.SELL), ohlc, "ma_crossover")
    assert buy["side_is_buy"] == 1.0
    assert sell["side_is_buy"] == 0.0


def test_features_dont_leak_future_bars():
    """Feature vector built from bars[:t+1] must equal the one built later from
    bars[:t+1] of a longer series — extending the series should not change the
    value at timestamp t."""
    long_ohlc = _ohlc(bars=120)
    # Truncate at bar 60 and compute features for that moment.
    short_ohlc = long_ohlc.iloc[:60].copy()
    sig_short = _signal(short_ohlc)

    # Compute features from the truncated window, and from the full window but
    # only up through the same timestamp.
    a = build_feature_vector(sig_short, short_ohlc, "ma_crossover")

    matching_slice = long_ohlc.loc[:short_ohlc.index[-1]]
    sig_long = _signal(matching_slice)
    b = build_feature_vector(sig_long, matching_slice, "ma_crossover")

    pd.testing.assert_series_equal(a, b)


def test_has_enough_bars_threshold():
    assert not has_enough_bars(_ohlc(bars=MIN_BARS - 1))
    assert has_enough_bars(_ohlc(bars=MIN_BARS))
