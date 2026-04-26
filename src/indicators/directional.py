"""Directional Movement Index — ADX, +DI, -DI (Wilder, 1978).

ADX measures trend strength regardless of direction (0–100). +DI and -DI
measure the strength of each directional pressure. The canonical rule:

  ADX ≥ 25   → trending market
  ADX < 20   → ranging / choppy
  +DI > -DI  → bullish pressure dominates

Wilder's smoothing is equivalent to an EWMA with alpha = 1/period, matching
the `atr()` helper in this module's `volatility` file.
"""
from __future__ import annotations

import pandas as pd


def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.DataFrame:
    """Return a DataFrame with columns: `adx`, `plus_di`, `minus_di`.

    All three are percentages (0–100). Values before `period` bars are NaN.
    """
    up = high.diff()
    down = -low.diff()

    plus_dm = ((up > down) & (up > 0)).astype(float) * up.clip(lower=0)
    minus_dm = ((down > up) & (down > 0)).astype(float) * down.clip(lower=0)

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr_ = _wilder_smooth(tr, period)
    plus_di = 100 * _wilder_smooth(plus_dm, period) / atr_
    minus_di = 100 * _wilder_smooth(minus_dm, period) / atr_

    # dx is undefined when +DI and -DI both collapse to 0 (flat price) — treat
    # as 0 rather than NaN so ADX converges on quiet markets instead of NaN-ing
    # through the later smoothing pass.
    denom = plus_di + minus_di
    dx = (100 * (plus_di - minus_di).abs() / denom).where(denom > 0, 0.0)
    adx_ = _wilder_smooth(dx, period)

    return pd.DataFrame({"adx": adx_, "plus_di": plus_di, "minus_di": minus_di})
