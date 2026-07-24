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

from src.indicators.directional import adx
from src.indicators.momentum import rsi, stochastic
from src.indicators.trend import ema, macd
from src.indicators.volatility import bollinger_bands


# Chart series palette. Indicator lines are deliberately NOT green/red:
# those two are reserved for P&L sign and for threshold guides, so a
# series never implies "good"/"bad" purely by its colour. Ordered by how
# fast the series reacts, which keeps multi-EMA charts readable.
# Mirrors the --accent/--green/--red tokens in the web + mobile themes.
C_FAST = "#38bdf8"  # fast series  — EMA8/12/20/21, %K, RSI, +DI
C_MID = "#a78bfa"   # mid series   — EMA26/50, %D, MACD signal, -DI
C_SLOW = "#f59e0b"  # slow series  — EMA55/200, ADX
C_BAND = "#64748b"  # envelopes and bands
C_OS = "#34d399"    # oversold guide   (signal green)
C_OB = "#f87171"    # overbought guide (signal red)


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
            "color": C_FAST,
            "values": _series_values(ema20, count),
        },
        {
            "name": "EMA50",
            "kind": "line",
            "color": C_MID,
            "values": _series_values(ema50, count),
        },
        {
            "name": "EMA200",
            "kind": "line",
            "color": C_SLOW,
            "values": _series_values(ema200, count),
        },
        {
            "name": "BB",
            "kind": "band",
            "color": C_BAND,
            "upper": _series_values(bb["upper"], count),
            "middle": _series_values(bb["middle"], count),
            "lower": _series_values(bb["lower"], count),
        },
    ]


# Strategy-specific decorations. Each entry returns (overlays, subplots)
# the bot will merge on top of the standard EMA+BB set. Overlays share
# the price axis; subplots get a separate pane below the candles.
def strategy_decorations(
    strategy_name: str,
    ohlc: pd.DataFrame,
    count: int = SNAPSHOT_BARS,
) -> tuple[list[dict], list[dict]]:
    if ohlc is None or len(ohlc) == 0:
        return [], []
    overlays: list[dict] = []
    subplots: list[dict] = []
    close = ohlc["close"]
    high = ohlc["high"]
    low = ohlc["low"]
    name = strategy_name or ""

    if name == "ma_crossover":
        overlays += [
            {"name": "EMA12", "kind": "line", "color": C_FAST,
             "values": _series_values(ema(close, 12), count), "emphasis": True},
            {"name": "EMA26", "kind": "line", "color": C_MID,
             "values": _series_values(ema(close, 26), count), "emphasis": True},
        ]

    elif name == "rsi_mean_reversion":
        subplots.append({
            "name": "RSI(14)", "kind": "line",
            "values": _series_values(rsi(close, 14), count),
            "color": C_FAST,
            "y_min": 0, "y_max": 100,
            "guides": [
                {"y": 30, "label": "OS", "color": C_OS},
                {"y": 70, "label": "OB", "color": C_OB},
            ],
        })

    elif name == "donchian_breakout":
        prior = ohlc.iloc[:-1]
        upper = float(prior["high"].rolling(20).max().iloc[-1])
        lower = float(prior["low"].rolling(20).min().iloc[-1])
        overlays += [
            {"name": "Channel high", "kind": "line", "color": C_FAST,
             "values": [upper] * min(count, len(ohlc)), "emphasis": True},
            {"name": "Channel low", "kind": "line", "color": C_MID,
             "values": [lower] * min(count, len(ohlc)), "emphasis": True},
        ]

    elif name == "macd_cross":
        m = macd(close, 12, 26, 9)
        subplots.append({
            "name": "MACD",
            "kind": "macd",
            "macd": _series_values(m["macd"], count),
            "signal": _series_values(m["signal"], count),
            "histogram": _series_values(m["histogram"], count),
            "color": C_FAST,
            "signal_color": C_MID,
        })

    elif name == "bollinger_bounce":
        # BB already drawn by standard set; add RSI subplot.
        subplots.append({
            "name": "RSI(14)", "kind": "line",
            "values": _series_values(rsi(close, 14), count),
            "color": C_FAST,
            "y_min": 0, "y_max": 100,
            "guides": [
                {"y": 35, "label": "OS", "color": C_OS},
                {"y": 65, "label": "OB", "color": C_OB},
            ],
        })

    elif name == "stochastic_reversal":
        st = stochastic(high, low, close, 14, 3)
        subplots.append({
            "name": "Stochastic",
            "kind": "double_line",
            "primary": _series_values(st["%K"], count),
            "secondary": _series_values(st["%D"], count),
            "primary_color": C_FAST,
            "secondary_color": C_MID,
            "y_min": 0, "y_max": 100,
            "guides": [
                {"y": 20, "label": "OS", "color": C_OS},
                {"y": 80, "label": "OB", "color": C_OB},
            ],
        })

    elif name == "triple_ma_alignment":
        overlays += [
            {"name": "EMA8", "kind": "line", "color": C_FAST,
             "values": _series_values(ema(close, 8), count), "emphasis": True},
            {"name": "EMA21", "kind": "line", "color": C_MID,
             "values": _series_values(ema(close, 21), count), "emphasis": True},
            {"name": "EMA55", "kind": "line", "color": C_SLOW,
             "values": _series_values(ema(close, 55), count), "emphasis": True},
        ]

    elif name == "inside_bar_breakout":
        # Mother bar = bar -3 (last is current, -2 is the inside bar).
        if len(ohlc) >= 3:
            mother = ohlc.iloc[-3]
            mh = float(mother["high"]); ml = float(mother["low"])
            overlays += [
                {"name": "Mother high", "kind": "line", "color": C_FAST,
                 "values": [mh] * min(count, len(ohlc)), "emphasis": True},
                {"name": "Mother low", "kind": "line", "color": C_MID,
                 "values": [ml] * min(count, len(ohlc)), "emphasis": True},
            ]

    elif name == "engulfing_pattern":
        overlays.append(
            {"name": "EMA50", "kind": "line", "color": C_MID,
             "values": _series_values(ema(close, 50), count), "emphasis": True}
        )

    elif name == "ema_pullback":
        overlays += [
            {"name": "EMA21", "kind": "line", "color": C_FAST,
             "values": _series_values(ema(close, 21), count), "emphasis": True},
            {"name": "EMA200", "kind": "line", "color": C_SLOW,
             "values": _series_values(ema(close, 200), count), "emphasis": True},
        ]

    elif name == "adx_breakout":
        adx_df = adx(high, low, close, 14)
        subplots.append({
            "name": "ADX(14)",
            "kind": "double_line",
            "primary": _series_values(adx_df["adx"], count),
            "secondary": _series_values(adx_df["plus_di"], count),
            "tertiary": _series_values(adx_df["minus_di"], count),
            "primary_color": C_SLOW,
            "secondary_color": C_FAST,
            "tertiary_color": C_MID,
            "y_min": 0, "y_max": 60,
            "guides": [
                {"y": 25, "label": "Trend", "color": C_SLOW},
            ],
        })
        # Lookback high/low markers on the price chart too.
        if len(ohlc) >= 21:
            prior = ohlc.iloc[:-1]
            upper = float(prior["high"].rolling(20).max().iloc[-1])
            lower = float(prior["low"].rolling(20).min().iloc[-1])
            overlays += [
                {"name": "Lookback high", "kind": "line", "color": C_FAST,
                 "values": [upper] * min(count, len(ohlc)), "emphasis": True},
                {"name": "Lookback low", "kind": "line", "color": C_MID,
                 "values": [lower] * min(count, len(ohlc)), "emphasis": True},
            ]

    return overlays, subplots
