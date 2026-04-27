"""PathRecorder — captures the bars a trade traversed from open to close.

Called once per close, after `journal.record_close`. Pulls the most
recent N bars from the data feed and persists the slice that overlaps
the trade's [opened_at, closed_at] window. Bars stored in the order the
feed returned them (chronological).

Best-effort: if the feed can't supply bars (mock with no history,
network failure on live), the recorder logs and returns. The replay
endpoint will then 404 for that trade, which the UI shows as 'no path
recorded'.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

import pandas as pd

from .path_store import PathBar, PathStore

log = logging.getLogger(__name__)


class _Feed(Protocol):
    def latest_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame: ...


class PathRecorder:
    def __init__(
        self,
        feed: _Feed,
        store: PathStore,
        timeframe: str,
        max_bars: int = 500,
    ) -> None:
        self.feed = feed
        self.store = store
        self.timeframe = timeframe
        # Caps the per-close fetch. 500 M15 bars ≈ 5 trading days — generous
        # enough for any sane hold period, small enough that the latest_bars
        # call stays cheap on MT5.
        self.max_bars = max_bars

    def record(
        self,
        trade_id: int,
        symbol: str,
        opened_at: datetime,
        closed_at: datetime,
    ) -> int:
        """Capture bars in [opened_at, closed_at]. Returns the count written.
        Returns 0 on any error or empty slice — never raises into the close path.
        """
        try:
            df = self.feed.latest_bars(symbol, self.timeframe, self.max_bars)
        except Exception:
            log.exception("path recorder: feed.latest_bars failed for trade %d", trade_id)
            return 0
        if df is None or df.empty:
            return 0

        try:
            sliced = self._slice(df, opened_at, closed_at)
        except Exception:
            log.exception("path recorder: slice failed for trade %d", trade_id)
            return 0
        if not sliced:
            return 0

        try:
            self.store.write(trade_id, sliced)
        except Exception:
            log.exception("path recorder: store.write failed for trade %d", trade_id)
            return 0
        return len(sliced)

    @staticmethod
    def _slice(df: pd.DataFrame, opened_at: datetime, closed_at: datetime) -> list[PathBar]:
        """Pick rows whose timestamp is in [opened_at, closed_at]. Falls back
        to a tail window (last 50) if the feed doesn't expose timestamps —
        the engine still gives a useful 'what if my stop was X' answer
        from the most recent bars.
        """
        # The mock feed and MT5DataFeed both return either a 'time' column or
        # a DatetimeIndex. Normalize to a chronologically-ordered iterable of
        # (ts, ohlc) tuples regardless.
        if "time" in df.columns:
            ts_series = pd.to_datetime(df["time"], utc=True)
        elif isinstance(df.index, pd.DatetimeIndex):
            ts_series = df.index.tz_convert("UTC") if df.index.tz else df.index.tz_localize("UTC")
        else:
            return _fallback_tail(df)

        opened = pd.Timestamp(opened_at).tz_convert("UTC") if opened_at.tzinfo else pd.Timestamp(opened_at, tz="UTC")
        closed = pd.Timestamp(closed_at).tz_convert("UTC") if closed_at.tzinfo else pd.Timestamp(closed_at, tz="UTC")
        mask = (ts_series >= opened) & (ts_series <= closed)
        sliced = df.loc[mask.values] if hasattr(mask, "values") else df.loc[mask]
        if sliced.empty:
            return _fallback_tail(df)

        out: list[PathBar] = []
        for ts, row in zip(ts_series[mask.values], sliced.itertuples(index=False)):
            out.append(PathBar(
                ts=str(ts),
                open=float(getattr(row, "open")),
                high=float(getattr(row, "high")),
                low=float(getattr(row, "low")),
                close=float(getattr(row, "close")),
            ))
        return out


def _fallback_tail(df: pd.DataFrame, n: int = 50) -> list[PathBar]:
    tail = df.tail(n)
    out: list[PathBar] = []
    for row in tail.itertuples(index=True):
        out.append(PathBar(
            ts=str(row.Index),
            open=float(getattr(row, "open")),
            high=float(getattr(row, "high")),
            low=float(getattr(row, "low")),
            close=float(getattr(row, "close")),
        ))
    return out
