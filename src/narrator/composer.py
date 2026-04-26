"""Composer — builds the prompt for a closed trade and persists the answer.

The prompt is assembled from data the bot already has on disk:
  * the journal row (entry/exit/PnL/close reason),
  * the explanation row (regime, allocator, signal levels at decision time),
  * the most recent fills for slippage/latency.

Output is a 2-3 sentence narrative aimed at the operator: what was the
setup, what happened, and what's the takeaway. Stored once; the API
serves from the store on subsequent reads.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .provider import LLMProvider
from .store import NarrativeStore, TradeNarrative

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are an analyst writing a 2-3 sentence post-mortem of a single forex "
    "trade for the operator. Lead with what the bot saw, what it did, and how "
    "it resolved. Be specific about pips, R-multiples, and the close reason. "
    "Do not invent details. Do not hedge or apologize. Plain text, no headers."
)


@dataclass(frozen=True)
class TradeContext:
    """Everything the composer needs to write the prompt. Pulled from the
    journal/explanations/fills tables by `gather()`. Held as a dataclass so
    tests can construct one directly without touching SQLite.
    """
    trade_id: int
    symbol: str
    side: str
    strategy: str
    lot_size: float
    entry_price: float
    exit_price: float | None
    stop_loss: float
    take_profit: float
    pnl: float
    close_reason: str | None
    opened_at: str
    closed_at: str | None
    # Derived from explanation row (may be None if explanations off).
    risk_reward: float | None = None
    stop_distance_pips: float | None = None
    regime_label: str | None = None
    allocator_role: str | None = None
    allocator_weight: float | None = None
    ml_filter_passed: bool | None = None
    # Aggregated from fills (may be None if exec-quality off or no fills).
    avg_slippage_pips: float | None = None
    avg_latency_ms: float | None = None


class NarratorComposer:
    """Wires provider + store + db readers. Single entry point: `narrate(trade_id)`.

    Reads are direct SQLite queries against the same `data/trades.db`. Keeping
    them inline (vs. depending on TradeJournal / TradeExplanationStore) avoids
    pulling those classes into a path that's strictly read-only — and avoids a
    circular dependency where `Bot` would have to inject its journal into the
    composer when narrate() runs from the close path.
    """

    def __init__(
        self,
        provider: LLMProvider,
        store: NarrativeStore,
        db_path: Path | str = "data/trades.db",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider = provider
        self.store = store
        self.db_path = Path(db_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def narrate(self, trade_id: int, *, force: bool = False) -> TradeNarrative | None:
        """Generate + persist a narrative for `trade_id`. Idempotent — if a
        narrative already exists, returns it unless `force=True`. Returns
        None when the trade isn't found or hasn't closed yet.
        """
        if not force:
            existing = self.store.get(trade_id)
            if existing is not None:
                return existing

        ctx = self.gather(trade_id)
        if ctx is None or ctx.closed_at is None:
            log.debug("narrator: trade %d not closed yet, skipping", trade_id)
            return None

        try:
            user = self.build_prompt(ctx)
            resp = self.provider.complete(_SYSTEM_PROMPT, user)
        except Exception:
            log.exception("narrator: provider failed for trade %d", trade_id)
            return None

        text = resp.text.strip()
        if not text:
            return None
        narrative = TradeNarrative(
            trade_id=trade_id,
            narrative=text,
            provider=self.provider.name,
            model=resp.model,
            prompt_tokens=resp.prompt_tokens,
            output_tokens=resp.output_tokens,
            created_at=self._clock().isoformat(),
        )
        self.store.write(narrative)
        return narrative

    def gather(self, trade_id: int) -> TradeContext | None:
        """Load journal + explanation + fills aggregates for one trade."""
        import sqlite3
        with sqlite3.connect(self.db_path) as c:
            c.row_factory = sqlite3.Row
            t = c.execute(
                "SELECT * FROM trades WHERE id = ?", (trade_id,),
            ).fetchone()
            if t is None:
                return None
            ex = c.execute(
                "SELECT * FROM trade_explanations WHERE trade_id = ?", (trade_id,),
            ).fetchone()
            # Fills table may not exist on minimal installations — guard the read.
            try:
                fills = c.execute(
                    """
                    SELECT AVG(slippage_pips) AS avg_slip, AVG(latency_ms) AS avg_lat
                    FROM fills WHERE trade_id = ?
                    """,
                    (trade_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                fills = None

        return TradeContext(
            trade_id=t["id"],
            symbol=t["symbol"],
            side=t["side"],
            strategy=t["strategy"],
            lot_size=t["lot_size"],
            entry_price=t["entry_price"],
            exit_price=t["exit_price"],
            stop_loss=t["stop_loss"],
            take_profit=t["take_profit"],
            pnl=t["pnl"],
            close_reason=t["close_reason"],
            opened_at=t["opened_at"],
            closed_at=t["closed_at"],
            risk_reward=ex["risk_reward"] if ex else None,
            stop_distance_pips=ex["stop_distance_pips"] if ex else None,
            regime_label=ex["regime_label"] if ex else None,
            allocator_role=ex["allocator_role"] if ex else None,
            allocator_weight=ex["allocator_weight"] if ex else None,
            ml_filter_passed=(
                bool(ex["ml_filter_passed"])
                if ex and ex["ml_filter_passed"] is not None else None
            ),
            avg_slippage_pips=(fills["avg_slip"] if fills and fills["avg_slip"] is not None else None),
            avg_latency_ms=(fills["avg_lat"] if fills and fills["avg_lat"] is not None else None),
        )

    def build_prompt(self, ctx: TradeContext) -> str:
        """Plain-text prompt. Headline numbers first so the stub provider
        and any prompt-truncating LLM still see the essentials.
        """
        lines: list[str] = []
        lines.append(
            f"Trade #{ctx.trade_id}: {ctx.side} {ctx.symbol} {ctx.lot_size:.2f} lots"
            f" via {ctx.strategy}"
        )
        if ctx.exit_price is not None:
            r = self._r_multiple(ctx)
            r_str = f", R={r:+.2f}" if r is not None else ""
            lines.append(
                f"Entry {ctx.entry_price:.5f} → exit {ctx.exit_price:.5f}, PnL {ctx.pnl:+.2f}{r_str}"
            )
        lines.append(
            f"Stop {ctx.stop_loss:.5f}, Target {ctx.take_profit:.5f}"
            + (f", Risk:Reward {ctx.risk_reward:.2f}" if ctx.risk_reward else "")
        )
        if ctx.close_reason:
            lines.append(f"Close reason: {ctx.close_reason}")
        if ctx.regime_label:
            lines.append(f"Regime at entry: {ctx.regime_label}")
        if ctx.allocator_role:
            w = f" (weight {ctx.allocator_weight:.2f})" if ctx.allocator_weight is not None else ""
            lines.append(f"Allocator: {ctx.allocator_role}{w}")
        if ctx.ml_filter_passed is not None:
            lines.append(f"ML filter: {'passed' if ctx.ml_filter_passed else 'blocked'}")
        if ctx.avg_slippage_pips is not None:
            lat = f" / {ctx.avg_latency_ms:.0f}ms" if ctx.avg_latency_ms is not None else ""
            lines.append(f"Avg slippage: {ctx.avg_slippage_pips:.2f} pips{lat}")
        lines.append(f"Opened {ctx.opened_at}, closed {ctx.closed_at}")
        return "\n".join(lines)

    @staticmethod
    def _r_multiple(ctx: TradeContext) -> float | None:
        if ctx.exit_price is None:
            return None
        risk = abs(ctx.entry_price - ctx.stop_loss)
        if risk <= 0:
            return None
        signed = (ctx.exit_price - ctx.entry_price) if ctx.side.upper() == "BUY" \
            else (ctx.entry_price - ctx.exit_price)
        return signed / risk
