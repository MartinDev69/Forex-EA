"""Smoke test for scripts/backtest.py — exercises arg parsing + BarStore I/O."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.data.bar_store import BarStore


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backtest.py"


@pytest.fixture
def cli():
    """Import scripts/backtest.py as a module so we can call main() in-process."""
    spec = importlib.util.spec_from_file_location("backtest_cli", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_bars(store: BarStore, symbol: str, bars: int = 400) -> None:
    rng = np.random.default_rng(3)
    close = 1.1 + np.cumsum(rng.normal(0, 0.0005, bars))
    idx = pd.date_range("2024-01-01", periods=bars, freq="15min", tz="UTC")
    df = pd.DataFrame(
        {"open": close, "high": close + 0.001, "low": close - 0.001,
         "close": close, "volume": 100.0},
        index=idx,
    )
    store.write(symbol, df)


def test_cli_writes_report_and_equity_curve(tmp_path: Path, cli):
    bars_dir = tmp_path / "bars"
    store = BarStore(bars_dir, format="parquet")
    _seed_bars(store, "EURUSD")

    out_dir = tmp_path / "reports"
    rc = cli.main([
        "--symbol", "EURUSD",
        "--strategy", "ma_crossover",
        "--bars-dir", str(bars_dir),
        "--out", str(out_dir),
        "--lookback", "50",
    ])
    assert rc == 0

    reports = list(out_dir.glob("EURUSD_ma_crossover_*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text())
    assert payload["symbol"] == "EURUSD"
    assert payload["strategy"] == "ma_crossover"
    assert "total_trades" in payload and "final_equity" in payload

    curve = reports[0].with_suffix(".equity.csv")
    assert curve.exists()


def test_cli_runs_all_strategies(tmp_path: Path, cli):
    bars_dir = tmp_path / "bars"
    store = BarStore(bars_dir, format="parquet")
    _seed_bars(store, "EURUSD", bars=500)

    out_dir = tmp_path / "reports"
    rc = cli.main([
        "--symbol", "EURUSD",
        "--strategy", "all",
        "--bars-dir", str(bars_dir),
        "--out", str(out_dir),
        "--lookback", "50",
    ])
    assert rc == 0
    # One report per strategy in the registry.
    from src.strategies import STRATEGY_REGISTRY
    reports = list(out_dir.glob("EURUSD_*.json"))
    assert len(reports) == len(STRATEGY_REGISTRY)


def test_cli_errors_when_bars_missing(tmp_path: Path, cli):
    bars_dir = tmp_path / "bars"
    bars_dir.mkdir()
    rc = cli.main([
        "--symbol", "GHOST",
        "--bars-dir", str(bars_dir),
        "--out", str(tmp_path / "reports"),
    ])
    assert rc == 2


def test_cli_rejects_unknown_strategy(tmp_path: Path, cli):
    bars_dir = tmp_path / "bars"
    store = BarStore(bars_dir, format="parquet")
    _seed_bars(store, "EURUSD")

    with pytest.raises(SystemExit, match="unknown strategy"):
        cli.main([
            "--symbol", "EURUSD",
            "--strategy", "bogus_thing",
            "--bars-dir", str(bars_dir),
            "--out", str(tmp_path / "reports"),
            "--lookback", "50",
        ])


def test_cli_respects_since_until_slice(tmp_path: Path, cli):
    bars_dir = tmp_path / "bars"
    store = BarStore(bars_dir, format="parquet")
    _seed_bars(store, "EURUSD", bars=400)

    rc = cli.main([
        "--symbol", "EURUSD",
        "--strategy", "ma_crossover",
        "--bars-dir", str(bars_dir),
        "--out", str(tmp_path / "reports"),
        "--since", "2024-01-02",
        "--until", "2024-01-04",
        "--lookback", "20",
    ])
    assert rc == 0
