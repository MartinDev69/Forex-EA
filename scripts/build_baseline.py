"""Seed drift baselines from a backtest run.

Backtests one or more strategies on stored bars and writes the resulting
performance metrics to the drift_baselines table. The DriftMonitor then
compares live trades against these numbers to detect alpha decay.

Usage:
  python scripts/build_baseline.py --symbol EURUSD --strategy ma_crossover \\
      --since 2023-01-01 --until 2024-01-01

  python scripts/build_baseline.py --symbol EURUSD --strategy all
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Put the repo root on sys.path so `python scripts/build_baseline.py …` works
# from the project directory without needing PYTHONPATH=. or running it as
# `python -m scripts.build_baseline`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.backtesting.engine import BacktestResult, run_backtest
from src.data.bar_store import BarStore
from src.drift import Baseline, BaselineStore
from src.strategies import STRATEGY_REGISTRY, Strategy

log = logging.getLogger("build_baseline")


def _parse_iso(s: str) -> datetime:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"bad date {s!r}: {e}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _slice_bars(bars: pd.DataFrame, since: datetime | None, until: datetime | None) -> pd.DataFrame:
    if since is not None:
        bars = bars[bars.index >= pd.Timestamp(since)]
    if until is not None:
        bars = bars[bars.index <= pd.Timestamp(until)]
    return bars


def _resolve_strategies(names: list[str], symbol: str) -> list[Strategy]:
    if "all" in names:
        return [cls(symbol) for cls in STRATEGY_REGISTRY.values()]
    out: list[Strategy] = []
    for name in names:
        cls = STRATEGY_REGISTRY.get(name)
        if cls is None:
            raise SystemExit(f"unknown strategy {name!r}; known: {list(STRATEGY_REGISTRY)}")
        out.append(cls(symbol))
    return out


def _baseline_from_result(result: BacktestResult, strategy: str, symbol: str) -> Baseline:
    """Pull the four metrics the DriftMonitor cares about out of a BacktestResult."""
    trades = result.trades
    closed = [t for t in trades if t.exit_time is not None]
    if not closed:
        raise SystemExit(f"backtest produced 0 closed trades for {strategy}/{symbol}")

    r_values: list[float] = []
    for t in closed:
        risk_per_unit = abs(t.entry_price - t.stop_loss)
        if risk_per_unit > 0 and t.exit_price is not None:
            move = t.exit_price - t.entry_price
            if t.side.value == "SELL":
                move = -move
            r_values.append(move / risk_per_unit)
    avg_r = sum(r_values) / len(r_values) if r_values else 0.0

    first = min(t.entry_time for t in closed)
    last = max(t.exit_time for t in closed if t.exit_time is not None)
    span_days = max(1.0, (last - first).total_seconds() / 86400.0)
    trades_per_day = len(closed) / span_days

    return Baseline(
        strategy=strategy,
        symbol=symbol,
        trade_count=len(closed),
        win_rate=result.win_rate,
        avg_r=avg_r,
        avg_trades_per_day=trades_per_day,
        source="backtest",
        computed_at=datetime.now(timezone.utc),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--strategy", default="all",
                        help=f"comma-separated names, or 'all'. Available: {','.join(STRATEGY_REGISTRY)}")
    parser.add_argument("--bars-dir", default="data/bars", type=Path)
    parser.add_argument("--format", choices=["parquet", "csv"], default=None)
    parser.add_argument("--since", type=_parse_iso, default=None)
    parser.add_argument("--until", type=_parse_iso, default=None)
    parser.add_argument("--starting-equity", type=float, default=10_000.0)
    parser.add_argument("--risk-per-trade", type=float, default=0.01)
    parser.add_argument("--lookback", type=int, default=100)
    parser.add_argument("--db-path", default=Path("data/trades.db"), type=Path)
    parser.add_argument(
        "--out-json", type=Path, default=None,
        help="If set, write baselines to this JSON file instead of/in addition "
             "to the SQLite store. Useful for running backtests on a fast "
             "machine and shipping the result to the slow VPS.",
    )
    parser.add_argument(
        "--no-db", action="store_true",
        help="Skip writing to the SQLite store. Implies --out-json is set.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.no_db and args.out_json is None:
        parser.error("--no-db only makes sense with --out-json")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    store = BarStore(args.bars_dir, format=args.format)
    bars = store.read(args.symbol)
    if bars is None or bars.empty:
        log.error("no bars for %s in %s", args.symbol, args.bars_dir)
        return 2

    bars = _slice_bars(bars, args.since, args.until)
    if len(bars) <= args.lookback:
        log.error("only %d bars after slicing, need > lookback=%d", len(bars), args.lookback)
        return 3

    strategies = _resolve_strategies([s.strip() for s in args.strategy.split(",")], args.symbol)
    baseline_store = None if args.no_db else BaselineStore(args.db_path)
    json_records: list[dict] = []

    rc = 0
    for strat in strategies:
        try:
            result = run_backtest(
                bars, strat,
                starting_equity=args.starting_equity,
                risk_per_trade_pct=args.risk_per_trade,
                lookback=args.lookback,
            )
        except Exception:
            log.exception("backtest failed for %s", strat.name)
            rc = 1
            continue

        try:
            baseline = _baseline_from_result(result, strat.name, args.symbol)
        except SystemExit as e:
            log.warning("%s", e)
            rc = 1
            continue

        if baseline_store is not None:
            baseline_store.upsert(baseline)
        if args.out_json is not None:
            json_records.append(baseline.to_dict())
        log.info(
            "[%s/%s] baseline computed: %d trades, win=%.1f%%, avg-R=%.2f, %.2f trades/day",
            strat.name, args.symbol, baseline.trade_count, baseline.win_rate * 100,
            baseline.avg_r, baseline.avg_trades_per_day,
        )

    # Merge into existing JSON (idempotent — same (strategy, symbol) is
    # replaced, others left alone). Lets you build the file up across
    # multiple symbol runs without losing prior baselines.
    if args.out_json is not None:
        import json
        path = args.out_json
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: list[dict] = []
        if path.is_file():
            try:
                existing = json.loads(path.read_text())
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                log.warning("couldn't parse existing %s — overwriting", path)
                existing = []
        keyed = {(r["strategy"], r["symbol"]): r for r in existing}
        for r in json_records:
            keyed[(r["strategy"], r["symbol"])] = r
        path.write_text(json.dumps(list(keyed.values()), indent=2))
        log.info("wrote %d baselines to %s (%d new this run)",
                 len(keyed), path, len(json_records))

    return rc


if __name__ == "__main__":
    sys.exit(main())
