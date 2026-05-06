"""One-shot reconciliation of journal trades closed by the broker.

If the bot is offline when a stop-loss / take-profit fires, MT5 closes the
position cleanly but the journal never sees the close — so the row stays
marked OPEN forever, with `exit_price`, `closed_at` and `pnl` blank. The
dashboard then shows "phantom open" rows that aren't real.

This script walks every journal row currently marked OPEN, asks MT5 for
that position's deal history, finds the closing deal(s), and writes back
the real exit price, close timestamp, and realized PnL (profit + swap +
commission). Idempotent — re-running it only touches rows that are still
OPEN in the journal AND already closed on MT5.

Usage:
    .\\venv\\Scripts\\python.exe scripts\\reconcile_closed_trades.py
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
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
            open_rows = list(conn.execute(
                """
                SELECT id, symbol, side, broker_ticket, entry_price, opened_at
                FROM trades
                WHERE status = 'OPEN' AND closed_at IS NULL
                  AND broker_ticket IS NOT NULL
                """
            ))

            if not open_rows:
                print("No journal trades marked OPEN — nothing to reconcile.")
                return

            print(f"Found {len(open_rows)} OPEN journal row(s); checking MT5 history…\n")

            reconciled = 0
            still_open = 0
            no_history = 0

            for row in open_rows:
                ticket = int(row["broker_ticket"])
                deals = mt5.history_deals_get(position=ticket)
                if deals is None or len(deals) == 0:
                    # No history at all — could mean it's truly still open OR
                    # the deals fell outside the default lookup window. Be safe.
                    still_open += 1
                    continue

                # If the position is still in MT5's open list, leave it alone.
                pos = mt5.positions_get(ticket=ticket)
                if pos:
                    still_open += 1
                    continue

                # entry == DEAL_ENTRY_OUT (1) is the close leg; INOUT (2) is a
                # reversal that closes the prior side. Both should count.
                close_deals = [
                    d for d in deals
                    if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT)
                ]
                if not close_deals:
                    # Has open deal but no close — the bot's MT5 view also
                    # missed it. Treat as still open.
                    no_history += 1
                    continue

                last = max(close_deals, key=lambda d: d.time)
                exit_price = float(last.price)
                closed_at = datetime.fromtimestamp(last.time, tz=timezone.utc)
                pnl = sum(
                    float(d.profit) + float(d.swap) + float(d.commission)
                    for d in close_deals
                )

                conn.execute(
                    """
                    UPDATE trades
                    SET status = 'CLOSED',
                        exit_price = ?,
                        closed_at = ?,
                        pnl = ?,
                        close_reason = COALESCE(close_reason, 'reconciled_from_mt5')
                    WHERE id = ?
                    """,
                    (exit_price, closed_at.isoformat(), pnl, row["id"]),
                )
                reconciled += 1
                sign = "+" if pnl >= 0 else ""
                print(
                    f"  ✓ #{ticket} {row['side']} {row['symbol']} "
                    f"{row['entry_price']} → {exit_price}  "
                    f"PnL {sign}{pnl:.2f}  closed {closed_at.isoformat()}"
                )

            conn.commit()

        print(
            f"\nDone. Reconciled {reconciled} closed trade(s); "
            f"{still_open} still actually open; {no_history} skipped (no close deal yet)."
        )
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
