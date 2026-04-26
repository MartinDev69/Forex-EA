"""Market-regime classifier.

Labels the latest bar with two orthogonal dimensions:
  - Trend: `trend_up` / `trend_down` / `range`
      Driven by ADX ≥ adx_trend_threshold and the +DI/-DI sign.
  - Volatility: `low` / `normal` / `high`
      Driven by the ATR's percentile rank inside a rolling lookback window.

Strategies can declare `preferred_regimes: set[TrendRegime]` so the bot skips
signals generated in conditions they don't handle well (e.g. mean-reversion
in a strong trend). The full snapshot is also surfaced to the dashboard so
operators can see why a strategy was gated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import pandas as pd

from src.indicators.directional import adx as adx_indicator
from src.indicators.volatility import atr


class TrendRegime(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    UNKNOWN = "unknown"


class VolatilityRegime(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RegimeConfig:
    adx_period: int = 14
    adx_trend_threshold: float = 22.0
    atr_period: int = 14
    atr_lookback: int = 100
    atr_low_pct: float = 0.30
    atr_high_pct: float = 0.70

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RegimeConfig":
        import os
        e = env if env is not None else os.environ
        return cls(
            adx_period=int(e.get("REGIME_ADX_PERIOD", "14")),
            adx_trend_threshold=float(e.get("REGIME_ADX_TREND_THRESHOLD", "22")),
            atr_period=int(e.get("REGIME_ATR_PERIOD", "14")),
            atr_lookback=int(e.get("REGIME_ATR_LOOKBACK", "100")),
            atr_low_pct=float(e.get("REGIME_ATR_LOW_PCT", "0.30")),
            atr_high_pct=float(e.get("REGIME_ATR_HIGH_PCT", "0.70")),
        )


@dataclass(frozen=True)
class RegimeSnapshot:
    trend: TrendRegime
    volatility: VolatilityRegime
    adx: float | None
    plus_di: float | None
    minus_di: float | None
    atr: float | None
    atr_pct: float | None  # rank in [0, 1] inside the lookback window
    timestamp: datetime | None

    @property
    def label(self) -> str:
        # `unknown` wins — we don't want to ship a confident-looking label
        # when we didn't actually have enough bars to classify.
        if self.trend == TrendRegime.UNKNOWN:
            return "unknown"
        return f"{self.trend.value}:{self.volatility.value}"

    def to_dict(self) -> dict:
        return {
            "trend": self.trend.value,
            "volatility": self.volatility.value,
            "label": self.label,
            "adx": self.adx,
            "plus_di": self.plus_di,
            "minus_di": self.minus_di,
            "atr": self.atr,
            "atr_pct": self.atr_pct,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


class RegimeClassifier:
    def __init__(self, config: RegimeConfig | None = None) -> None:
        self.config = config or RegimeConfig()

    def classify(self, ohlc: pd.DataFrame) -> RegimeSnapshot:
        cfg = self.config
        min_bars = max(cfg.adx_period * 2, cfg.atr_lookback) + 2
        if len(ohlc) < min_bars:
            return RegimeSnapshot(
                trend=TrendRegime.UNKNOWN,
                volatility=VolatilityRegime.UNKNOWN,
                adx=None, plus_di=None, minus_di=None,
                atr=None, atr_pct=None,
                timestamp=(ohlc.index[-1].to_pydatetime() if len(ohlc) else None),
            )

        di = adx_indicator(ohlc["high"], ohlc["low"], ohlc["close"], cfg.adx_period)
        atr_series = atr(ohlc["high"], ohlc["low"], ohlc["close"], cfg.atr_period)

        last_adx = di["adx"].iloc[-1]
        last_plus = di["plus_di"].iloc[-1]
        last_minus = di["minus_di"].iloc[-1]
        last_atr = atr_series.iloc[-1]

        if pd.isna(last_adx) or pd.isna(last_atr):
            return RegimeSnapshot(
                trend=TrendRegime.UNKNOWN,
                volatility=VolatilityRegime.UNKNOWN,
                adx=None, plus_di=None, minus_di=None,
                atr=None, atr_pct=None,
                timestamp=ohlc.index[-1].to_pydatetime(),
            )

        # Trend decision
        if last_adx >= cfg.adx_trend_threshold:
            trend = TrendRegime.TREND_UP if last_plus >= last_minus else TrendRegime.TREND_DOWN
        else:
            trend = TrendRegime.RANGE

        # Volatility decision — ATR's percentile rank inside the lookback window.
        window = atr_series.iloc[-cfg.atr_lookback:].dropna()
        atr_pct: float | None
        if len(window) >= 10:
            atr_pct = float((window <= last_atr).mean())
            if atr_pct <= cfg.atr_low_pct:
                vol = VolatilityRegime.LOW
            elif atr_pct >= cfg.atr_high_pct:
                vol = VolatilityRegime.HIGH
            else:
                vol = VolatilityRegime.NORMAL
        else:
            atr_pct = None
            vol = VolatilityRegime.UNKNOWN

        return RegimeSnapshot(
            trend=trend,
            volatility=vol,
            adx=float(last_adx),
            plus_di=float(last_plus) if not pd.isna(last_plus) else None,
            minus_di=float(last_minus) if not pd.isna(last_minus) else None,
            atr=float(last_atr),
            atr_pct=atr_pct,
            timestamp=ohlc.index[-1].to_pydatetime(),
        )
