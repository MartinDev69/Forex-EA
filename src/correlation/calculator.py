"""Pairwise correlation of log returns across a basket of symbols.

Pure compute — given a dict of `symbol -> close-price Series`, return a
correlation DataFrame. The Bot owns the data feed and is responsible for
producing those Series; this module is intentionally feed-agnostic so it can
be unit-tested with synthetic data.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CorrelationConfig:
    window_bars: int = 200
    min_observations: int = 50

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "CorrelationConfig":
        import os
        e = env if env is not None else os.environ
        return cls(
            window_bars=int(e.get("CORRELATION_WINDOW_BARS", "200")),
            min_observations=int(e.get("CORRELATION_MIN_OBS", "50")),
        )


class CorrelationCalculator:
    def __init__(self, config: CorrelationConfig | None = None) -> None:
        self.config = config or CorrelationConfig()

    def matrix(self, closes: dict[str, pd.Series]) -> pd.DataFrame:
        """Return a symmetric correlation DataFrame indexed by symbol.

        - Closes are aligned on their datetime index (inner join). Missing bars
          on one symbol drop the matching row across the basket — Pearson
          correlation has no defensible value otherwise.
        - Returns are computed as np.log(close / close.shift(1)). The first
          observation is dropped along with any NaNs from misalignment.
        - Symbols with fewer than `min_observations` return rows after
          alignment are dropped from the output entirely; the caller can tell
          a symbol was skipped because it won't appear as an index/column.
        """
        if not closes:
            return pd.DataFrame()

        cfg = self.config
        df = pd.concat(
            {sym: s.tail(cfg.window_bars) for sym, s in closes.items()},
            axis=1, join="inner",
        )
        # Inner-join can leave us with very few rows if one symbol is sparse —
        # bail out cleanly rather than returning a noisy 2-bar correlation.
        if len(df) < cfg.min_observations + 1:
            return pd.DataFrame()

        # log returns; first row is NaN by construction
        returns = np.log(df / df.shift(1)).iloc[1:]

        usable = [sym for sym in returns.columns if returns[sym].notna().sum() >= cfg.min_observations]
        if len(usable) < 2:
            return pd.DataFrame()

        return returns[usable].corr(min_periods=cfg.min_observations)
