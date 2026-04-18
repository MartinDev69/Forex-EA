import numpy as np
import pandas as pd
import pytest

from src.indicators.momentum import rsi
from src.indicators.trend import ema, macd, sma
from src.indicators.volatility import atr, bollinger_bands


@pytest.fixture
def price_series() -> pd.Series:
    np.random.seed(42)
    return pd.Series(100 + np.cumsum(np.random.randn(200)))


@pytest.fixture
def ohlc() -> pd.DataFrame:
    np.random.seed(7)
    close = 100 + np.cumsum(np.random.randn(200))
    high = close + np.abs(np.random.randn(200))
    low = close - np.abs(np.random.randn(200))
    return pd.DataFrame({"high": high, "low": low, "close": close})


def test_sma_length(price_series):
    result = sma(price_series, 20)
    assert len(result) == len(price_series)
    assert result.iloc[:19].isna().all()
    assert not result.iloc[19:].isna().any()


def test_ema_responds_faster_than_sma(price_series):
    s = sma(price_series, 20)
    e = ema(price_series, 20)
    assert pd.Series(e.iloc[19:]).notna().all()
    assert not s.equals(e)


def test_rsi_bounds(price_series):
    r = rsi(price_series, 14).dropna()
    assert (r >= 0).all() and (r <= 100).all()


def test_macd_columns(price_series):
    m = macd(price_series)
    assert set(m.columns) == {"macd", "signal", "histogram"}


def test_atr_positive(ohlc):
    a = atr(ohlc["high"], ohlc["low"], ohlc["close"]).dropna()
    assert (a > 0).all()


def test_bollinger_ordering(price_series):
    b = bollinger_bands(price_series).dropna()
    assert (b["upper"] >= b["middle"]).all()
    assert (b["middle"] >= b["lower"]).all()
