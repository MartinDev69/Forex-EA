"""Snapshot helpers — turn the bars the strategy was looking at into
serialisable JSON the dashboard can render as a candlestick chart with
indicator overlays.

We compute a standard overlay set (EMA20/50/200 + Bollinger Bands) on
every captured chart so users get useful context regardless of which
strategy fired. NaN values get coerced to None on the wire so the
chart renderer can skip them rather than draw zero.
"""
from __future__ import annotations

import math

import pandas as pd

from src.indicators.trend import ema
from src.indicators.volatility import bollinger_bands


# Number of trailing bars to capture per signal. 50 is enough to draw
# a meaningful chart with EMAs warmed up, without bloating the journal.
SNAPSHOT_BARS = 50


def _coerce(value: float) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def serialise_bars(ohlc: pd.DataFrame, count: int = SNAPSHOT_BARS) -> list[dict]:
    """Take the last `count` rows and return [{t, o, h, l, c}].

    Empty list if the frame doesn't have enough rows or expected columns.
    """
    if ohlc is None or len(ohlc) == 0:
        return []
    needed = {"open", "high", "low", "close"}
    if not needed.issubset(ohlc.columns):
        return []
    sub = ohlc.tail(count)
    out: list[dict] = []
    for ts, row in sub.iterrows():
        try:
            t = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        except Exception:
            t = str(ts)
        out.append({
            "t": t,
            "o": _coerce(row["open"]),
            "h": _coerce(row["high"]),
            "l": _coerce(row["low"]),
            "c": _coerce(row["close"]),
        })
    return out


def _series_values(series: pd.Series, count: int) -> list[float | None]:
    if series is None or len(series) == 0:
        return [None] * count
    return [_coerce(v) for v in series.tail(count).tolist()]


def standard_overlays(ohlc: pd.DataFrame, count: int = SNAPSHOT_BARS) -> list[dict]:
    """Compute the default overlay set every chart shows: three EMAs
    plus a Bollinger band envelope. Each overlay returns

        { name, kind: 'line'|'band', color, values | upper/lower/middle }

    so the chart renderer can decide how to draw it.
    """
    if ohlc is None or len(ohlc) < 20:
        return []
    try:
        close = ohlc["close"]
        ema20 = ema(close, 20)
        ema50 = ema(close, 50)
        ema200 = ema(close, 200)
        bb = bollinger_bands(close, 20, 2.0)
    except Exception:
        return []

    return [
        {
            "name": "EMA20",
            "kind": "line",
            "color": "#22ee88",
            "values": _series_values(ema20, count),
        },
        {
            "name": "EMA50",
            "kind": "line",
            "color": "#ffc73a",
            "values": _series_values(ema50, count),
        },
        {
            "name": "EMA200",
            "kind": "line",
            "color": "#ff3355",
            "values": _series_values(ema200, count),
        },
        {
            "name": "BB",
            "kind": "band",
            "color": "#8fa0aa",
            "upper": _series_values(bb["upper"], count),
            "middle": _series_values(bb["middle"], count),
            "lower": _series_values(bb["lower"], count),
        },
    ]
