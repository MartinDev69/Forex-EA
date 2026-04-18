import numpy as np
import pandas as pd

from src.strategies.base import SignalType
from src.strategies.breakout import DonchianBreakoutStrategy


def _ohlc(close: np.ndarray, high=None, low=None) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(close), freq="15min")
    if high is None:
        high = close + 0.5
    if low is None:
        low = close - 0.5
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": 100},
        index=idx,
    )


def test_buy_on_upside_breakout():
    # Stable range around 100, then a spike above the channel high on the last bar.
    flat = np.full(40, 100.0)
    close = np.concatenate([flat, np.array([102.0])])
    # Make the last bar's high breach the prior 20-bar high (100.5).
    high = np.concatenate([flat + 0.5, np.array([102.5])])
    low = np.concatenate([flat - 0.5, np.array([100.0])])
    ohlc = _ohlc(close, high=high, low=low)

    strat = DonchianBreakoutStrategy("EURUSD", channel_period=20, atr_period=14)
    signal = strat.generate_signal(ohlc)

    assert signal.type == SignalType.BUY
    assert signal.stop_loss is not None and signal.stop_loss < signal.price
    assert signal.take_profit is not None and signal.take_profit > signal.price


def test_sell_on_downside_breakout():
    flat = np.full(40, 100.0)
    close = np.concatenate([flat, np.array([98.0])])
    high = np.concatenate([flat + 0.5, np.array([100.0])])
    low = np.concatenate([flat - 0.5, np.array([97.5])])
    ohlc = _ohlc(close, high=high, low=low)

    strat = DonchianBreakoutStrategy("EURUSD", channel_period=20, atr_period=14)
    signal = strat.generate_signal(ohlc)

    assert signal.type == SignalType.SELL
    assert signal.stop_loss is not None and signal.stop_loss > signal.price
    assert signal.take_profit is not None and signal.take_profit < signal.price


def test_hold_inside_channel():
    close = np.full(50, 100.0)
    ohlc = _ohlc(close)
    strat = DonchianBreakoutStrategy("EURUSD")
    assert strat.generate_signal(ohlc).type == SignalType.HOLD


def test_invalid_period_rejected():
    try:
        DonchianBreakoutStrategy("EURUSD", channel_period=1)
    except ValueError:
        return
    raise AssertionError("expected ValueError")
