"""BarStore — roundtrip, dedup, merge semantics."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.data.bar_store import BAR_COLUMNS, BarStore


def _bars(n: int = 10, start: str = "2024-01-01", tz: str | None = "UTC") -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="15min", tz=tz)
    rng = np.random.default_rng(0)
    close = 1.1 + np.cumsum(rng.normal(0, 0.0001, n))
    return pd.DataFrame(
        {
            "open": close, "high": close + 0.001, "low": close - 0.001,
            "close": close, "volume": 100.0,
        },
        index=idx,
    )


@pytest.fixture(params=["parquet", "csv"])
def store(request, tmp_path: Path) -> BarStore:
    return BarStore(tmp_path / "bars", format=request.param)


def test_roundtrip(store: BarStore):
    bars = _bars(20)
    added = store.write("EURUSD", bars)
    assert added == 20

    got = store.read("EURUSD")
    assert got is not None
    assert list(got.columns) == list(BAR_COLUMNS)
    assert len(got) == 20
    pd.testing.assert_index_equal(got.index, bars.index)


def test_read_missing_returns_none(store: BarStore):
    assert store.read("GHOSTPAIR") is None
    assert store.last_timestamp("GHOSTPAIR") is None
    assert store.has("GHOSTPAIR") is False


def test_write_merges_and_dedups(store: BarStore):
    first = _bars(10, start="2024-01-01")
    store.write("EURUSD", first)

    # Second batch overlaps the last 3 bars and adds 5 new ones.
    second = _bars(8, start="2024-01-01 01:45")  # bar 7 of first -> last 3 overlap
    added = store.write("EURUSD", second)

    got = store.read("EURUSD")
    assert got is not None
    # Overlapping rows replaced, not duplicated.
    assert got.index.is_unique
    assert got.index.is_monotonic_increasing
    # 10 + 8 - 3 overlap = 15 total; added = 15 - 10 = 5.
    assert len(got) == 15
    assert added == 5


def test_write_overwrites_existing_rows(store: BarStore):
    first = _bars(5)
    store.write("EURUSD", first)

    # Rewrite the same range with distinct values.
    updated = first.copy()
    updated["close"] = 9.999
    store.write("EURUSD", updated)

    got = store.read("EURUSD")
    assert (got["close"] == 9.999).all()
    assert len(got) == 5


def test_last_timestamp(store: BarStore):
    store.write("EURUSD", _bars(5))
    ts = store.last_timestamp("EURUSD")
    assert ts is not None
    assert ts.tzinfo is not None
    assert ts == pd.Timestamp("2024-01-01 01:00", tz="UTC")


def test_write_rejects_missing_columns(store: BarStore):
    bad = _bars(5).drop(columns=["volume"])
    with pytest.raises(ValueError, match="missing required columns"):
        store.write("EURUSD", bad)


def test_write_rejects_non_datetime_index(store: BarStore):
    bad = _bars(5).reset_index(drop=True)
    with pytest.raises(ValueError, match="DatetimeIndex"):
        store.write("EURUSD", bad)


def test_naive_index_gets_localized_to_utc(store: BarStore):
    """MT5 ingester hands us tz-aware bars, but users may hand-build naive ones —
    the store should not refuse those; it localizes to UTC for consistency."""
    bars = _bars(5, tz=None)
    store.write("EURUSD", bars)
    got = store.read("EURUSD")
    assert got.index.tz is not None


def test_format_fallback_when_parquet_unavailable(tmp_path: Path, monkeypatch):
    from src.data import bar_store as mod
    monkeypatch.setattr(mod, "_PARQUET_OK", False)
    s = BarStore(tmp_path, format=None)
    assert s.format == "csv"
    with pytest.raises(RuntimeError, match="pyarrow not installed"):
        BarStore(tmp_path, format="parquet")
