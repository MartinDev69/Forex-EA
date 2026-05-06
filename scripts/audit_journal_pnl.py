"""Audit closed journal trades against MT5's actual deal profit.

The bot used to estimate PnL with a 4-decimal-FX formula, which silently
multiplied USOIL / XAU / index profits by ~100×. That's how a $39.30
USOILm scalp ended up logged as +$3,930 in the journal.

This script walks every CLOSED row that has a broker_ticket, asks MT5
for the matching position's deal history, and patches the row if the
journal pnl differs from the broker's reported profit by more than a
small tolerance. Idempotent — re-running on a clean journal is a no-op.

Usage:
    .\\venv\\Scripts\\python.exe scripts\\audit_journal_pnl.py            # patch in place
    .\\venv\\Scripts\\python.exe scripts\\audit_journal_pnl.py --dry-run  # report only
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Tolerance for matching journal PnL to MT5's reported profit. Rounding
# in the journal vs broker swap-cron timing means a tiny drift is OK.
TOLERANCE = 0.05


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="report mismatches but don't write")
    args = parser.parse_args()

    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise SystemExit(
            "MetaTrader5 module not installed — run from the bot's venv on the VPS."
        )

    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")

    try:
        db_path = Path("data/trades.db")
        if not db_path.exists():
            raise SystemExit(f"Journal DB not found at {db_path} — wrong cwd?")

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = list(conn.execute(
                """
                SELECT id, symbol, side, entry_price, exit_price, pnl, broker_ticket
                FROM trades
                WHERE status = 'CLOSED' AND broker_ticket IS NOT NULL
                ORDER BY closed_at
                """
            ))

            if not rows:
                print("No closed journal rows with broker_ticket — nothing to audit.")
                return

            checked = 0
            patched = 0
            mismatches: list[tuple[sqlite3.Row, float, float]] = []
            no_history = 0

            for row in rows:
                ticket = int(row["broker_ticket"])
                deals = mt5.history_deals_get(position=ticket)
                if not deals:
                    no_history += 1
                    continue
                out_entries = (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT)
                close_deals = [d for d in deals if d.entry in out_entries]
                if not close_deals:
                    no_history += 1
                    continue
                actual = sum(
                    float(d.profit) + float(d.swap) + float(d.commission)
                    for d in close_deals
                )
                checked += 1
                journal = float(row["pnl"] or 0.0)
                if abs(actual - journal) <= TOLERANCE:
                    continue
                mismatches.append((row, journal, actual))

            if not mismatches:
                print(f"All {checked} closed rows match MT5 within ${TOLERANCE:.2f}. "
                      f"Journal is clean.")
                return

            print(f"Found {len(mismatches)} mismatch(es) out of {checked} checked:\n")
            net_journal = 0.0
            net_actual = 0.0
            for row, journal, actual in mismatches:
                ratio = actual / journal if journal else 0.0
                ratio_str = f"  ratio≈{ratio:.4f}" if journal else ""
                print(
                    f"  #{row['broker_ticket']} {row['side']} {row['symbol']} "
                    f"{row['entry_price']} → {row['exit_price']}  "
                    f"journal={journal:+.2f}  mt5={actual:+.2f}{ratio_str}"
                )
                net_journal += journal
                net_actual += actual

            delta = net_actual - net_journal
            sign = "+" if delta >= 0 else ""
            print(
                f"\nNet correction: journal totaled {net_journal:+.2f}, "
                f"MT5 reports {net_actual:+.2f} ({sign}{delta:.2f} adjustment)."
            )

            if args.dry_run:
                print("\nDry-run — no changes written.")
                return

            for row, _journal, actual in mismatches:
                conn.execute(
                    "UPDATE trades SET pnl = ? WHERE id = ?",
                    (actual, row["id"]),
                )
                patched += 1
            conn.commit()
            print(f"\nPatched {patched} row(s). Refresh the dashboard to see the truth.")
            if no_history:
                print(f"({no_history} row(s) had no MT5 history — left alone.)")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
