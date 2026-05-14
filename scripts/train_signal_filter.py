"""Train the XGBoost signal filter from closed trades in the journal.

Usage:
  python scripts/train_signal_filter.py \
      --db data/trades.db \
      --bars-dir data/bars \
      --out data/models/signal_filter.json

`--bars-dir` must contain one OHLC file per symbol, named {SYMBOL}.parquet
(preferred) or {SYMBOL}.csv with a DatetimeIndex and open/high/low/close/volume
columns. For every trade, we slice bars up to `opened_at` so the training set
sees exactly what the live filter would see.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# Put the repo root on sys.path so `python scripts/train_signal_filter.py …` works.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.execution.journal import TradeJournal
from src.ml.signal_filter import SignalFilter
from src.ml.training import build_training_dataset, train, walk_forward_evaluate

log = logging.getLogger("train_signal_filter")


def _load_symbol_bars(bars_dir: Path, symbol: str) -> pd.DataFrame:
    for suffix, reader in (
        (".parquet", pd.read_parquet),
        (".csv", lambda p: pd.read_csv(p, index_col=0, parse_dates=True)),
    ):
        path = bars_dir / f"{symbol}{suffix}"
        if path.exists():
            df = reader(path)
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
            return df
    raise FileNotFoundError(f"no bars file for {symbol} in {bars_dir}")


def _make_bar_lookup(bars_dir: Path):
    cache: dict[str, pd.DataFrame] = {}

    def get_bars(symbol: str, until: datetime) -> pd.DataFrame:
        if symbol not in cache:
            cache[symbol] = _load_symbol_bars(bars_dir, symbol)
        bars = cache[symbol]
        # Slice up to and including `until` — no future leak.
        return bars.loc[:pd.Timestamp(until)]

    return get_bars


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/trades.db", type=Path)
    parser.add_argument("--bars-dir", default="data/bars", type=Path)
    parser.add_argument("--out", default="data/models/signal_filter.json", type=Path)
    parser.add_argument("--limit", default=10_000, type=int)
    parser.add_argument("--test-size", default=0.2, type=float)
    parser.add_argument("--walk-forward", type=int, default=5,
                        help="folds for time-series CV (0 disables, use random split only)")
    parser.add_argument("--wf-window", choices=["expanding", "sliding"], default="expanding")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.db.exists():
        log.error("journal db not found at %s", args.db)
        return 2
    if not args.bars_dir.exists():
        log.error("bars dir not found at %s — see module docstring for layout", args.bars_dir)
        return 2

    journal = TradeJournal(args.db)
    get_bars = _make_bar_lookup(args.bars_dir)

    log.info("Building training dataset from %s (limit=%d)", args.db, args.limit)
    X, y, ts = build_training_dataset(journal, get_bars, limit=args.limit)

    if len(X) == 0:
        log.error("no labeled trades could be turned into features — nothing to train on")
        return 3

    log.info("Dataset: %d samples (%d wins, %d losses)", len(X), int(y.sum()), int((1 - y).sum()))

    # Walk-forward CV first — this tells us how the model generalizes across time.
    wf_payload: dict | None = None
    if args.walk_forward >= 2 and len(X) >= args.walk_forward + 1:
        log.info("Walk-forward validation: %d folds (%s window)", args.walk_forward, args.wf_window)
        wf = walk_forward_evaluate(X, y, ts, n_folds=args.walk_forward, window=args.wf_window)
        for f in wf.folds:
            log.info("  fold %d: train=%d test=%d acc=%.3f auc=%.3f (%s → %s)",
                     f.fold, f.train_size, f.test_size, f.accuracy, f.auc,
                     f.test_start.date(), f.test_end.date())
        log.info("Walk-forward mean: acc=%.3f auc=%.3f (±%.3f)",
                 wf.mean_accuracy, wf.mean_auc, wf.std_auc)
        wf_payload = {
            "n_folds": len(wf.folds),
            "mean_accuracy": wf.mean_accuracy,
            "mean_auc": wf.mean_auc,
            "std_auc": wf.std_auc,
            "folds": [
                {"fold": f.fold, "train_size": f.train_size, "test_size": f.test_size,
                 "accuracy": f.accuracy, "auc": f.auc,
                 "test_start": f.test_start.isoformat(), "test_end": f.test_end.isoformat()}
                for f in wf.folds
            ],
        }
    elif args.walk_forward >= 2:
        log.warning("Not enough samples (%d) for %d walk-forward folds — skipping CV",
                    len(X), args.walk_forward)

    # Final model fits on everything — CV above estimates how it'll actually behave.
    model, report = train(X, y, test_size=args.test_size)
    log.info(
        "Random-split (sanity-check only): accuracy=%.3f auc=%.3f",
        report.test_accuracy, report.test_auc,
    )
    top = sorted(report.feature_importance.items(), key=lambda kv: kv[1], reverse=True)[:8]
    log.info("Top features: %s", top)

    sf = SignalFilter(model=model)
    sf.save(args.out)
    log.info("Model saved to %s", args.out)

    report_path = args.out.with_suffix(".report.json")
    report_body = {
        "n_samples": report.n_samples,
        "n_wins": report.n_wins,
        "n_losses": report.n_losses,
        "random_split_accuracy": report.test_accuracy,
        "random_split_auc": report.test_auc,
        "feature_importance": report.feature_importance,
    }
    if wf_payload is not None:
        report_body["walk_forward"] = wf_payload
    report_path.write_text(json.dumps(report_body, indent=2))
    log.info("Report saved to %s", report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
