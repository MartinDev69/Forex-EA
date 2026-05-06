"""Backfill the journal from MT5 deal history.

The bot's journal can drift behind the broker for a bunch of reasons:
- early runs before the journal-id fix dropped INSERTs silently
- positions opened/closed manually in the MT5 terminal
- bot crashes that lost trades between record_open and record_close
- broker-side stop-outs while the bot was offline

This script walks the last N days of MT5 deal history, groups deals by
position_id, and inserts any closed positions that are missing from the
journal. Trades that already exist (matched by broker_ticket) are left
alone — the script is idempotent.

Usage:
    .\\venv\\Scripts\\python.exe scripts\\backfill_history.py             # 30 days
    .\\venv\\Scripts\\python.exe scripts\\backfill_history.py --days 90    # 90 days
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30,
                        help="how far back to scan MT5 history (default: 30)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be inserted, but don't write")
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
        date_to = datetime.now(tz=timezone.utc)
        date_from = date_to - timedelta(days=args.days)
        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            print(f"history_deals_get returned None — {mt5.last_error()}")
            return
        if not deals:
            print(f"No deals in the last {args.days} days.")
            return

        # Group by position_id. A position has an OPEN deal (entry==IN) and
        # one or more CLOSE deals (entry==OUT). Skip balance ops (position 0).
        by_pos: dict[int, list] = {}
        for d in deals:
            pid = int(d.position_id)
            if pid == 0:
                continue
            by_pos.setdefault(pid, []).append(d)

        db_path = Path("data/trades.db")
        if not db_path.exists():
            raise SystemExit(f"Journal DB not found at {db_path} — wrong cwd?")

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            existing = {
                int(r["broker_ticket"]) for r in conn.execute(
                    "SELECT broker_ticket FROM trades WHERE broker_ticket IS NOT NULL"
                )
            }

            inserted = 0
            updated_close = 0
            still_open = 0
            skipped_existing = 0

            for pid, pdeals in by_pos.items():
                pdeals.sort(key=lambda d: d.time)
                opens = [d for d in pdeals if d.entry == mt5.DEAL_ENTRY_IN]
                closes = [
                    d for d in pdeals
                    if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT)
                ]
                if not opens:
                    continue  # synthetic / partial we can't anchor

                open_d = opens[0]
                still_active = not closes
                if still_active:
                    still_open += 1
                    if pid in existing:
                        continue  # already tracked as open by import_open_positions
                    # Insert as OPEN — bot will manage on next tick.
                    side = "BUY" if open_d.type == mt5.DEAL_TYPE_BUY else "SELL"
                    opened_at = datetime.fromtimestamp(open_d.time, tz=timezone.utc)
                    print(
                        f"  + open #{pid} {side} {open_d.symbol} "
                        f"{open_d.volume} @ {open_d.price}"
                    )
                    if not args.dry_run:
                        conn.execute(
                            """
                            INSERT INTO trades (id, symbol, side, lot_size,
                                entry_price, stop_loss, take_profit, strategy,
                                status, opened_at, broker_ticket)
                            VALUES (?, ?, ?, ?, ?, 0, 0, 'imported_history',
                                'OPEN', ?, ?)
                            """,
                            (
                                pid, open_d.symbol, side, float(open_d.volume),
                                float(open_d.price), opened_at.isoformat(), pid,
                            ),
                        )
                        inserted += 1
                    continue

                # Closed position. Compute aggregate exit info.
                last_close = max(closes, key=lambda d: d.time)
                exit_price = float(last_close.price)
                closed_at = datetime.fromtimestamp(last_close.time, tz=timezone.utc)
                pnl = sum(
                    float(d.profit) + float(d.swap) + float(d.commission)
                    for d in closes
                )
                side = "BUY" if open_d.type == mt5.DEAL_TYPE_BUY else "SELL"
                opened_at = datetime.fromtimestamp(open_d.time, tz=timezone.utc)
                sign = "+" if pnl >= 0 else ""

                if pid in existing:
                    # Row already in journal. If it's still marked OPEN,
                    # patch the close. Otherwise leave alone.
                    cur = conn.execute(
                        "SELECT status FROM trades WHERE broker_ticket = ?", (pid,)
                    ).fetchone()
                    if cur and cur["status"] == "OPEN":
                        print(
                            f"  ✓ patch close #{pid} {side} {open_d.symbol} "
                            f"{open_d.price} → {exit_price}  "
                            f"PnL {sign}{pnl:.2f}"
                        )
                        if not args.dry_run:
                            conn.execute(
                                """
                                UPDATE trades
                                SET status = 'CLOSED',
                                    exit_price = ?,
                                    closed_at = ?,
                                    pnl = ?,
                                    close_reason = COALESCE(close_reason, 'reconciled_from_mt5')
                                WHERE broker_ticket = ?
                                """,
                                (exit_price, closed_at.isoformat(), pnl, pid),
                            )
                            updated_close += 1
                    else:
                        skipped_existing += 1
                    continue

                # Truly missing — insert as a closed historical row.
                print(
                    f"  + closed #{pid} {side} {open_d.symbol} "
                    f"{open_d.price} → {exit_price}  "
                    f"PnL {sign}{pnl:.2f}  ({opened_at.date()} → {closed_at.date()})"
                )
                if not args.dry_run:
                    conn.execute(
                        """
                        INSERT INTO trades (id, symbol, side, lot_size,
                            entry_price, exit_price, stop_loss, take_profit,
                            strategy, status, opened_at, closed_at, pnl,
                            close_reason, broker_ticket)
                        VALUES (?, ?, ?, ?, ?, ?, 0, 0, 'imported_history',
                            'CLOSED', ?, ?, ?, 'imported_history', ?)
                        """,
                        (
                            pid, open_d.symbol, side, float(open_d.volume),
                            float(open_d.price), exit_price,
                            opened_at.isoformat(), closed_at.isoformat(),
                            pnl, pid,
                        ),
                    )
                    inserted += 1

            if not args.dry_run:
                conn.commit()

        flag = " (dry-run, no writes)" if args.dry_run else ""
        print(
            f"\nDone{flag}. Inserted {inserted} historical row(s); "
            f"patched close on {updated_close}; "
            f"{still_open} still open in MT5; "
            f"{skipped_existing} already-closed rows left alone."
        )
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
