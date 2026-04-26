"""Historical bar ingestion from MetaTrader 5.

MT5 caps `copy_rates_range` at ~100k bars per call, so for multi-year M1/M5
history we page backwards in chunks. `update()` reads the store, figures out
where it left off, and fetches only newer bars — idempotent, so rerunning is cheap.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .bar_store import BAR_COLUMNS, BarStore

log = logging.getLogger(__name__)


TIMEFRAME_MAP = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 16385, "H4": 16388, "D1": 16408, "W1": 32769, "MN1": 49153,
}

# Minutes per bar — used to estimate chunk sizes. Matches TIMEFRAME_MAP keys.
TIMEFRAME_MINUTES = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D1": 1440, "W1": 10080, "MN1": 43200,
}


def _load_mt5():
    try:
        import MetaTrader5 as mt5  # type: ignore
        return mt5
    except ImportError:
        return None


class MT5Ingester:
    def __init__(
        self,
        mt5_module: Any | None = None,
        chunk_days: int = 30,
    ) -> None:
        self._mt5 = mt5_module if mt5_module is not None else _load_mt5()
        if self._mt5 is None:
            raise RuntimeError(
                "MetaTrader5 is not available. Install it on a Windows host, "
                "or inject a fake module for testing."
            )
        self.chunk_days = chunk_days

    # ------------------------------------------------------------------ fetch

    def fetch(
        self,
        symbol: str,
        timeframe: str,
        since: datetime,
        until: datetime | None = None,
    ) -> pd.DataFrame:
        """Return all bars between `since` and `until` (UTC). Chunks internally."""
        tf = TIMEFRAME_MAP.get(timeframe.upper())
        if tf is None:
            raise ValueError(f"Unknown timeframe: {timeframe}")

        since = _ensure_utc(since)
        until = _ensure_utc(until) if until else datetime.now(timezone.utc)
        if since >= until:
            return _empty_bars()

        all_chunks: list[pd.DataFrame] = []
        cursor = since
        while cursor < until:
            chunk_end = min(cursor + timedelta(days=self.chunk_days), until)
            rates = self._mt5.copy_rates_range(symbol, tf, cursor, chunk_end)
            if rates is None or len(rates) == 0:
                log.debug("mt5 returned no rates for %s %s %s..%s",
                          symbol, timeframe, cursor.isoformat(), chunk_end.isoformat())
            else:
                all_chunks.append(_rates_to_frame(rates))
            cursor = chunk_end

        if not all_chunks:
            return _empty_bars()
        df = pd.concat(all_chunks)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df[list(BAR_COLUMNS)]

    # ------------------------------------------------------------------ update

    def update(
        self,
        store: BarStore,
        symbol: str,
        timeframe: str,
        since: datetime | None = None,
    ) -> int:
        """Fetch newer bars and merge them into the store. Returns rows added.

        If `since` is None, resume from the store's last timestamp. If the
        store is empty and `since` is None, we default to 90 days back — enough
        for the ML trainer to have a useful window without a painful cold start.
        """
        last = store.last_timestamp(symbol)
        if since is None:
            if last is not None:
                minutes = TIMEFRAME_MINUTES.get(timeframe.upper(), 15)
                since = (last + timedelta(minutes=minutes)).to_pydatetime()
            else:
                since = datetime.now(timezone.utc) - timedelta(days=90)

        log.info("ingester: fetching %s %s from %s", symbol, timeframe, since.isoformat())
        bars = self.fetch(symbol, timeframe, since)
        if bars.empty:
            log.info("ingester: no new bars for %s %s", symbol, timeframe)
            return 0

        added = store.write(symbol, bars)
        stored = store.read(symbol)
        total = 0 if stored is None else len(stored)
        log.info("ingester: %s %s added=%d, total=%d",
                 symbol, timeframe, added, total)
        return added


# --- helpers ----------------------------------------------------------------

def _rates_to_frame(rates) -> pd.DataFrame:
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time")
    # MT5 uses tick_volume (trade count) or real_volume (exchange volume).
    volume_col = "real_volume" if "real_volume" in df.columns and df["real_volume"].any() else "tick_volume"
    return df.rename(columns={volume_col: "volume"})[list(BAR_COLUMNS)]


def _empty_bars() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], tz="UTC")
    return pd.DataFrame({c: [] for c in BAR_COLUMNS}, index=idx).astype("float64")


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
