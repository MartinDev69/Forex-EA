"""One-shot resync of MT5 open positions back into the bot's journal.

When the journal-id bug was live (every MT5 trade tried to INSERT with
id=0), the second order onwards landed on MT5 successfully but the
journal write raised IntegrityError. Those positions are still open on
the broker but invisible to the bot — it can't trail-stop them, can't
close them at target, can't include them in correlation/heat math.

Run this once after fixing the id bug. It walks every open MT5 position
on the account, looks up whether the journal already has a row keyed by
broker_ticket, and inserts any that are missing. Idempotent — running it
twice is a no-op.

Usage:
    .\\venv\\Scripts\\python.exe scripts\\import_open_positions.py
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
            "MetaTrader5 module not installed — this script must run on the "
            "Windows VPS with the bot's venv activated."
        )

    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")

    try:
        positions = mt5.positions_get()
        if positions is None:
            print("MT5 positions_get returned None — terminal busy or no perms.")
            return
        if not positions:
            print("No open positions on the account. Nothing to import.")
            return

        db_path = Path("data/trades.db")
        if not db_path.exists():
            raise SystemExit(f"Journal DB not found at {db_path} — wrong cwd?")

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            existing_tickets = {
                row["broker_ticket"] for row in conn.execute(
                    "SELECT broker_ticket FROM trades WHERE broker_ticket IS NOT NULL"
                )
            }

            imported = 0
            skipped = 0
            for p in positions:
                ticket = int(p.ticket)
                if ticket in existing_tickets:
                    skipped += 1
                    continue

                side = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
                opened_at = datetime.fromtimestamp(p.time, tz=timezone.utc)
                strategy = (p.comment or "imported_from_mt5").strip() or "imported_from_mt5"
                conn.execute(
                    """
                    INSERT INTO trades (id, symbol, side, lot_size, entry_price,
                        stop_loss, take_profit, strategy, status, opened_at,
                        broker_ticket)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                    """,
                    (
                        ticket, p.symbol, side, float(p.volume), float(p.price_open),
                        float(p.sl) if p.sl else 0.0,
                        float(p.tp) if p.tp else 0.0,
                        strategy, opened_at.isoformat(), ticket,
                    ),
                )
                imported += 1
                print(f"  + imported #{ticket} {side} {p.symbol} {p.volume} @ {p.price_open}")

            conn.commit()

        print(
            f"\nDone. {imported} position(s) imported into the journal, "
            f"{skipped} already tracked."
        )
        if imported:
            print(
                "The bot will now manage these on its next tick — trailing stops, "
                "TP/SL closes, correlation heat all apply."
            )
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
