"""Run one or more strategies against stored bars and print a summary.

Usage:
  python scripts/backtest.py --symbol EURUSD --strategy ma_crossover
  python scripts/backtest.py --symbol EURUSD --strategy all \\
      --since 2023-01-01 --until 2024-01-01 --out data/backtests/

Reads bars via BarStore (parquet preferred, CSV fallback). Writes a JSON
report + equity-curve CSV per run so you can diff strategies over time.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

# Put the repo root on sys.path so `python scripts/backtest.py …` works.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.backtesting.engine import BacktestResult, run_backtest
from src.data.bar_store import BarStore
from src.strategies import STRATEGY_REGISTRY, Strategy

log = logging.getLogger("backtest")


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


def _write_report(result: BacktestResult, path: Path, symbol: str, strategy_name: str,
                  starting_equity: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    trades_payload = []
    for t in result.trades:
        d = asdict(t)
        # Timestamps + enums need string serialization for JSON.
        for k in ("entry_time", "exit_time"):
            d[k] = d[k].isoformat() if d[k] is not None else None
        d["side"] = t.side.value
        trades_payload.append(d)
    path.write_text(json.dumps({
        "symbol": symbol,
        "strategy": strategy_name,
        "starting_equity": starting_equity,
        "final_equity": result.final_equity,
        "total_return": result.total_return,
        "total_trades": result.total_trades,
        "win_rate": result.win_rate,
        "profit_factor": result.profit_factor,
        "trades": trades_payload,
    }, indent=2))

    curve_path = path.with_suffix(".equity.csv")
    result.equity_curve.to_csv(curve_path, header=["equity"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--strategy", default="all",
                        help="comma-separated names, or 'all'. "
                             f"Available: {','.join(STRATEGY_REGISTRY)}")
    parser.add_argument("--bars-dir", default="data/bars", type=Path)
    parser.add_argument("--format", choices=["parquet", "csv"], default=None)
    parser.add_argument("--since", type=_parse_iso, default=None)
    parser.add_argument("--until", type=_parse_iso, default=None)
    parser.add_argument("--starting-equity", type=float, default=10_000.0)
    parser.add_argument("--risk-per-trade", type=float, default=0.01)
    parser.add_argument("--lookback", type=int, default=100)
    parser.add_argument("--out", type=Path, default=Path("data/backtests"))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    store = BarStore(args.bars_dir, format=args.format)
    bars = store.read(args.symbol)
    if bars is None or bars.empty:
        log.error("no bars for %s in %s — run scripts/fetch_bars.py first", args.symbol, args.bars_dir)
        return 2

    bars = _slice_bars(bars, args.since, args.until)
    if len(bars) <= args.lookback:
        log.error("only %d bars after slicing, need > lookback=%d", len(bars), args.lookback)
        return 3

    strategies = _resolve_strategies([s.strip() for s in args.strategy.split(",")], args.symbol)
    log.info("Backtesting %s on %d bars (%s → %s) with %d strategy/ies",
             args.symbol, len(bars), bars.index[0].date(), bars.index[-1].date(), len(strategies))

    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
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
        log.info("[%s] %s", strat.name, result.summary())
        out_path = args.out / f"{args.symbol}_{strat.name}_{run_stamp}.json"
        _write_report(result, out_path, args.symbol, strat.name, args.starting_equity)
        log.info("  → %s", out_path)
    return rc


if __name__ == "__main__":
    sys.exit(main())
