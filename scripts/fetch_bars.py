"""Backfill historical bars from MT5 into data/bars/.

Usage (on the Windows VPS where MT5 is installed):
  python scripts/fetch_bars.py --symbols EURUSD,GBPUSD --timeframe M15
  python scripts/fetch_bars.py --symbols EURUSD --since 2023-01-01

Rerunning is safe — the ingester resumes from the last stored bar and only
fetches newer rows. The training pipeline reads from the same files.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.connection.mt5_client import MT5Client
from src.data.bar_store import BarStore
from src.data.mt5_ingester import MT5Ingester

log = logging.getLogger("fetch_bars")


def _parse_iso(s: str) -> datetime:
    # Accept "2024-01-01" or "2024-01-01T00:00:00Z"
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"bad date {s!r}: {e}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True,
                        help="comma-separated list (e.g. EURUSD,GBPUSD)")
    parser.add_argument("--timeframe", default="M15")
    parser.add_argument("--since", type=_parse_iso, default=None,
                        help="ISO date/time, UTC. Omit to resume from last bar.")
    parser.add_argument("--out-dir", default="data/bars", type=Path)
    parser.add_argument("--format", choices=["parquet", "csv"], default=None)
    parser.add_argument("--mt5-login", type=int, default=None)
    parser.add_argument("--mt5-password", default=None)
    parser.add_argument("--mt5-server", default=None)
    parser.add_argument("--mt5-path", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from src.config import load_settings
    settings = load_settings()

    login = args.mt5_login or settings.mt5_login
    password = args.mt5_password or settings.mt5_password
    server = args.mt5_server or settings.mt5_server
    path = args.mt5_path or settings.mt5_path

    if not (login and password and server):
        log.error("MT5 credentials missing — set --mt5-* flags or .env variables.")
        return 2

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    store = BarStore(args.out_dir, format=args.format)

    with MT5Client(login=login, password=password, server=server, path=path or None):
        # MT5Client has already called mt5.initialize(); the ingester can grab
        # the same module via its default loader.
        ingester = MT5Ingester()
        total_added = 0
        for symbol in symbols:
            try:
                added = ingester.update(store, symbol, args.timeframe, since=args.since)
                total_added += added
                log.info("%s: +%d bars (file=%s)", symbol, added, store.path_for(symbol))
            except Exception:
                log.exception("fetch failed for %s", symbol)

    log.info("Done. Total bars added: %d", total_added)
    return 0


if __name__ == "__main__":
    sys.exit(main())
