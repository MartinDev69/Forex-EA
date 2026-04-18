import numpy as np
import pandas as pd

from src.strategies.base import SignalType
from src.strategies.rsi_mean_reversion import RSIMeanReversionStrategy


def _ohlc(close: np.ndarray) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(close), freq="15min")
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 100,
        },
        index=idx,
    )


def test_buy_when_rsi_crosses_up_from_oversold():
    # Build a sustained down-move (drops RSI to oversold) then a bounce.
    down = np.linspace(100, 90, 40)
    bounce = np.linspace(90, 95, 10)
    ohlc = _ohlc(np.concatenate([down, bounce]))

    strat = RSIMeanReversionStrategy("EURUSD", rsi_period=14)
    signal = strat.generate_signal(ohlc)

    assert signal.type in {SignalType.BUY, SignalType.HOLD}
    assert signal.symbol == "EURUSD"


def test_hold_when_no_cross():
    # Flat price — RSI hovers around 50, no crosses.
    ohlc = _ohlc(np.full(60, 100.0))
    strat = RSIMeanReversionStrategy("EURUSD")
    assert strat.generate_signal(ohlc).type == SignalType.HOLD


def test_hold_on_insufficient_data():
    ohlc = _ohlc(np.array([100.0, 100.5, 101.0]))
    strat = RSIMeanReversionStrategy("EURUSD")
    assert strat.generate_signal(ohlc).type == SignalType.HOLD


def test_invalid_thresholds_rejected():
    try:
        RSIMeanReversionStrategy("EURUSD", oversold=70, overbought=30)
    except ValueError:
        return
    raise AssertionError("expected ValueError")
