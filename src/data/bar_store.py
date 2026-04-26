"""On-disk historical OHLC storage.

One file per symbol, parquet-by-default, CSV as a fallback when pyarrow isn't
available. The training pipeline, the backtester, and the fetch CLI all read
and write through this interface.

Schema (what the rest of the bot expects):
  * Index: pd.DatetimeIndex, tz=UTC, monotonically increasing, unique
  * Columns: open, high, low, close, volume (float64)

Merging:
  Overlapping timestamps from a new write overwrite existing rows — assumption
  is that the latest fetch is authoritative. Gaps are preserved (we don't
  interpolate); downstream code decides how to handle them.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import pandas as pd

log = logging.getLogger(__name__)

BAR_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")

# Parquet needs pyarrow (or fastparquet). If neither is available we fall back
# to CSV so the store still works on minimal installs — slower but correct.
try:
    import pyarrow  # noqa: F401
    _PARQUET_OK = True
except ImportError:  # pragma: no cover
    _PARQUET_OK = False


Format = Literal["parquet", "csv"]


class BarStore:
    def __init__(
        self,
        root_dir: Path | str,
        format: Format | None = None,
    ) -> None:
        """`format=None` picks parquet if pyarrow is available, else CSV.

        Storing the format choice at construction time means a single store
        instance won't straddle formats — callers that want mixed modes
        should instantiate twice.
        """
        self.root = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        if format is None:
            format = "parquet" if _PARQUET_OK else "csv"
        if format == "parquet" and not _PARQUET_OK:
            raise RuntimeError("pyarrow not installed — pass format='csv' or install pyarrow.")
        self.format: Format = format

    # ------------------------------------------------------------------ paths

    def path_for(self, symbol: str) -> Path:
        return self.root / f"{symbol}.{self.format}"

    def has(self, symbol: str) -> bool:
        return self.path_for(symbol).exists()

    # ------------------------------------------------------------------ read

    def read(self, symbol: str) -> pd.DataFrame | None:
        path = self.path_for(symbol)
        if not path.exists():
            return None
        if self.format == "parquet":
            df = pd.read_parquet(path)
        else:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
        df = _normalize(df)
        return df

    def last_timestamp(self, symbol: str) -> pd.Timestamp | None:
        df = self.read(symbol)
        if df is None or df.empty:
            return None
        return df.index[-1]

    # ------------------------------------------------------------------ write

    def write(self, symbol: str, bars: pd.DataFrame) -> int:
        """Merge `bars` into the existing file and return the number of new rows added.

        Existing rows with the same timestamp are overwritten by the incoming
        data. The total row count delta is what we report — the caller can
        compare against `len(bars)` to gauge how much overlapped.
        """
        new = _validate(bars)
        existing = self.read(symbol)
        if existing is None:
            merged = new
            added = len(new)
        else:
            before = len(existing)
            # Concat, keep last occurrence for duplicates, sort by timestamp.
            merged = pd.concat([existing, new])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            added = len(merged) - before

        self._persist(symbol, merged)
        log.debug("bar_store: wrote %s (%d rows total, +%d new)", symbol, len(merged), added)
        return added

    # ------------------------------------------------------------------ internals

    def _persist(self, symbol: str, df: pd.DataFrame) -> None:
        path = self.path_for(symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.format == "parquet":
            df.to_parquet(path)
        else:
            df.to_csv(path)


# --- helpers ----------------------------------------------------------------

def _validate(bars: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in BAR_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"bars missing required columns: {missing}")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("bars must have a DatetimeIndex")
    df = _normalize(bars)
    return df[list(BAR_COLUMNS)]


def _normalize(bars: pd.DataFrame) -> pd.DataFrame:
    """Ensure UTC index, sorted, deduped, float64 OHLCV."""
    df = bars.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    elif df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    for col in BAR_COLUMNS:
        if col in df.columns:
            df[col] = df[col].astype("float64")
    return df
