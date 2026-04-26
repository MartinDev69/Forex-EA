"""MT5Ingester — chunking, resume-from-last, timeframe handling.

Uses a fake mt5 module so we don't need MetaTrader5 installed. The fake lets
tests assert the exact (symbol, start, end) ranges the ingester asked for.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.data.bar_store import BarStore
from src.data.mt5_ingester import MT5Ingester


class _FakeMT5:
    """Records calls, returns synthetic bars for the requested range."""

    def __init__(self, bars_per_minute_pair: dict[tuple[str, int], pd.DataFrame] | None = None):
        self.calls: list[tuple[str, int, datetime, datetime]] = []
        self._canned = bars_per_minute_pair or {}
        self._last_error = (0, "ok")

    def copy_rates_range(self, symbol, tf, start, end):
        self.calls.append((symbol, tf, start, end))

        canned = self._canned.get((symbol, tf))
        if canned is not None:
            subset = canned.loc[(canned.index >= start) & (canned.index < end)]
            if subset.empty:
                return None
            return _frame_to_rates(subset)

        # Default: produce minute-resolution bars scaled to the timeframe code.
        step_minutes = 15 if tf == 15 else 60
        idx = pd.date_range(start, end, freq=f"{step_minutes}min", tz="UTC", inclusive="left")
        if len(idx) == 0:
            return None
        close = np.linspace(1.1, 1.2, len(idx))
        df = pd.DataFrame(
            {
                "time": idx.view("int64") // 1_000_000_000,
                "open": close, "high": close + 0.001, "low": close - 0.001,
                "close": close, "tick_volume": 100, "spread": 1, "real_volume": 0,
            }
        )
        return df.to_records(index=False)

    def last_error(self):
        return self._last_error


def _frame_to_rates(df: pd.DataFrame):
    out = df.reset_index().rename(columns={"index": "time"})
    out["time"] = (out["time"].view("int64") // 1_000_000_000).astype("int64")
    out["tick_volume"] = out.get("volume", 100)
    out["real_volume"] = 0
    out["spread"] = 1
    cols = ["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]
    return out[cols].to_records(index=False)


# ---------------------------------------------------------------- fetch


def test_fetch_chunks_across_long_ranges():
    fake = _FakeMT5()
    ing = MT5Ingester(mt5_module=fake, chunk_days=7)

    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    until = datetime(2024, 1, 22, tzinfo=timezone.utc)  # 21 days -> 3 chunks at 7 days each
    df = ing.fetch("EURUSD", "M15", since, until)

    assert len(fake.calls) == 3
    symbols = {c[0] for c in fake.calls}
    assert symbols == {"EURUSD"}
    # First chunk starts at `since`; chunks are contiguous.
    assert fake.calls[0][2] == since
    assert fake.calls[0][3] == since + timedelta(days=7)
    assert fake.calls[1][2] == since + timedelta(days=7)
    assert fake.calls[-1][3] == until

    assert not df.empty
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is not None


def test_fetch_empty_when_since_after_until():
    fake = _FakeMT5()
    ing = MT5Ingester(mt5_module=fake)
    df = ing.fetch("EURUSD", "M15",
                   datetime(2024, 1, 10, tzinfo=timezone.utc),
                   datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert df.empty
    assert fake.calls == []


def test_fetch_rejects_unknown_timeframe():
    fake = _FakeMT5()
    ing = MT5Ingester(mt5_module=fake)
    with pytest.raises(ValueError, match="Unknown timeframe"):
        ing.fetch("EURUSD", "M7", datetime(2024, 1, 1, tzinfo=timezone.utc))


def test_fetch_handles_empty_chunks():
    fake = _FakeMT5()
    # Null-returning fake: empty frame
    fake.copy_rates_range = lambda *a, **kw: None
    ing = MT5Ingester(mt5_module=fake, chunk_days=7)
    df = ing.fetch("EURUSD", "M15",
                   datetime(2024, 1, 1, tzinfo=timezone.utc),
                   datetime(2024, 1, 8, tzinfo=timezone.utc))
    assert df.empty


# ---------------------------------------------------------------- update


def test_update_resumes_from_last_timestamp(tmp_path: Path):
    fake = _FakeMT5()
    ing = MT5Ingester(mt5_module=fake, chunk_days=7)
    store = BarStore(tmp_path, format="parquet")

    # Seed the store with two existing bars.
    seed_idx = pd.date_range("2024-01-01", periods=2, freq="15min", tz="UTC")
    seed = pd.DataFrame(
        {
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100.0,
        },
        index=seed_idx,
    )
    store.write("EURUSD", seed)

    # Update should request bars starting AFTER the last seeded bar.
    ing.update(store, "EURUSD", "M15")
    assert fake.calls, "update should have called copy_rates_range"
    start = fake.calls[0][2]
    # Seed ends at 2024-01-01 00:15, next M15 bar is 00:30.
    expected = datetime(2024, 1, 1, 0, 30, tzinfo=timezone.utc)
    assert start == expected


def test_update_cold_start_default_lookback(tmp_path: Path):
    fake = _FakeMT5()
    ing = MT5Ingester(mt5_module=fake, chunk_days=30)
    store = BarStore(tmp_path, format="parquet")

    ing.update(store, "EURUSD", "H1")
    assert fake.calls
    start = fake.calls[0][2]
    # Default is 90 days back; allow ±1 day for clock drift.
    age_days = (datetime.now(timezone.utc) - start).days
    assert 89 <= age_days <= 91


def test_update_explicit_since_overrides(tmp_path: Path):
    fake = _FakeMT5()
    ing = MT5Ingester(mt5_module=fake, chunk_days=7)
    store = BarStore(tmp_path, format="parquet")
    store.write("EURUSD", _seed_bars_for_update())

    since = datetime(2024, 2, 1, tzinfo=timezone.utc)
    ing.update(store, "EURUSD", "M15", since=since)

    # Even though the store has later bars, explicit `since` wins.
    assert fake.calls[0][2] == since


def test_update_no_new_bars_returns_zero(tmp_path: Path):
    fake = _FakeMT5()
    # Fake returns nothing — simulates "no new bars since last fetch".
    fake.copy_rates_range = lambda *a, **kw: None
    ing = MT5Ingester(mt5_module=fake, chunk_days=7)
    store = BarStore(tmp_path, format="parquet")
    store.write("EURUSD", _seed_bars_for_update())

    added = ing.update(store, "EURUSD", "M15")
    assert added == 0


def _seed_bars_for_update() -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=5, freq="15min", tz="UTC")
    return pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100.0},
        index=idx,
    )


def test_ingester_raises_without_mt5():
    with pytest.raises(RuntimeError, match="MetaTrader5 is not available"):
        MT5Ingester(mt5_module=None)
