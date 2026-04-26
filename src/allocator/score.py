"""Per-(strategy, symbol) score derived from recent closed trades.

The allocator reads these scores to decide which variant gets full risk
weight (champion) and which get a smaller probe weight (challenger).

Cost profile: one indexed `LIMIT N` query per known (strategy, symbol) pair.
Returns zero-sample placeholders for pairs with no trades yet so the caller
can still seed an entry in the allocation store.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class StrategyScore:
    strategy: str
    symbol: str
    sample_size: int
    avg_r: float       # realized R, signed by side, in stop-multiples
    win_rate: float    # 0.0 - 1.0
    computed_at: str   # ISO-8601 UTC

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "sample_size": self.sample_size,
            "avg_r": self.avg_r,
            "win_rate": self.win_rate,
            "computed_at": self.computed_at,
        }


def score_pairs(
    db_path: Path | str,
    pairs: Iterable[tuple[str, str]],
    window: int = 30,
) -> list[StrategyScore]:
    """Score each (strategy, symbol) pair from the last `window` closed trades.

    Pairs with no trades return a zero-sample StrategyScore so the allocator
    can still record a 'no data' decision rather than silently skipping.
    """
    now = datetime.now(timezone.utc).isoformat()
    out: list[StrategyScore] = []
    conn = sqlite3.connect(Path(db_path))
    conn.row_factory = sqlite3.Row
    try:
        for strategy, symbol in pairs:
            rows = conn.execute(
                """
                SELECT entry_price, stop_loss, exit_price, pnl, side
                FROM trades
                WHERE strategy = ? AND symbol = ? AND status = 'CLOSED'
                ORDER BY closed_at DESC
                LIMIT ?
                """,
                (strategy, symbol, window),
            ).fetchall()
            out.append(_score_rows(strategy, symbol, rows, now))
    finally:
        conn.close()
    return out


def _score_rows(
    strategy: str,
    symbol: str,
    rows: list[sqlite3.Row],
    now: str,
) -> StrategyScore:
    if not rows:
        return StrategyScore(
            strategy=strategy,
            symbol=symbol,
            sample_size=0,
            avg_r=0.0,
            win_rate=0.0,
            computed_at=now,
        )

    wins = 0
    r_values: list[float] = []
    for r in rows:
        if (r["pnl"] or 0.0) > 0:
            wins += 1
        risk_per_unit = abs(r["entry_price"] - r["stop_loss"])
        if risk_per_unit > 0 and r["exit_price"] is not None:
            move = r["exit_price"] - r["entry_price"]
            if r["side"] == "SELL":
                move = -move
            r_values.append(move / risk_per_unit)

    avg_r = sum(r_values) / len(r_values) if r_values else 0.0
    win_rate = wins / len(rows)
    return StrategyScore(
        strategy=strategy,
        symbol=symbol,
        sample_size=len(rows),
        avg_r=avg_r,
        win_rate=win_rate,
        computed_at=now,
    )
