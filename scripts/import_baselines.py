"""Import drift baselines from a JSON file into the live trades.db.

Companion to ``build_baseline.py --out-json``. The workflow is:

  1. On the VPS, fetch bars once with ``fetch_bars.py`` (needs MT5).
  2. Copy ``data/bars/*.parquet`` to your Mac (commit + pull, or scp).
  3. On the Mac (5-10x faster than the VPS):
       python scripts/build_baseline.py --symbol EURUSDm --strategy all \\
           --since 2021-01-01 --until 2026-01-01 \\
           --out-json data/baselines.json --no-db
  4. Commit ``data/baselines.json``, push.
  5. On the VPS:
       git pull
       python scripts/import_baselines.py data/baselines.json

Each record is upserted by (strategy, symbol) — re-running is safe.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# Put the repo root on sys.path so `python scripts/import_baselines.py …` works.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.drift import Baseline, BaselineStore  # noqa: E402

log = logging.getLogger("import_baselines")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "json_path", type=Path,
        help="Path to baselines.json produced by build_baseline.py --out-json",
    )
    parser.add_argument(
        "--db-path", default=Path("data/trades.db"), type=Path,
        help="Target SQLite. Defaults to the live trades.db.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.json_path.is_file():
        log.error("not found: %s", args.json_path)
        return 2

    try:
        records = json.loads(args.json_path.read_text())
    except json.JSONDecodeError as exc:
        log.error("invalid JSON: %s", exc)
        return 3
    if not isinstance(records, list):
        log.error("expected a JSON array at top level, got %s", type(records).__name__)
        return 4

    store = BaselineStore(args.db_path)
    imported = 0
    for r in records:
        try:
            baseline = Baseline(
                strategy=r["strategy"],
                symbol=r["symbol"],
                trade_count=int(r["trade_count"]),
                win_rate=float(r["win_rate"]),
                avg_r=float(r["avg_r"]),
                avg_trades_per_day=float(r["avg_trades_per_day"]),
                source=r.get("source", "backtest"),
                computed_at=datetime.fromisoformat(r["computed_at"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("skipping malformed record %r: %s", r, exc)
            continue
        store.upsert(baseline)
        imported += 1
        log.info(
            "[%s/%s] %d trades · win=%.1f%% · avg-R=%.2f · %.2f trades/day",
            baseline.strategy, baseline.symbol, baseline.trade_count,
            baseline.win_rate * 100, baseline.avg_r, baseline.avg_trades_per_day,
        )
    log.info("imported %d of %d baselines into %s",
             imported, len(records), args.db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
