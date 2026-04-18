"""Bot orchestrator — the event loop that ties strategies, risk, and execution.

Design:
  - Pluggable DataFeed + Executor (mock locally, MT5 on Windows VPS)
  - One pass per bar per symbol — we track the last processed bar timestamp
    so a strategy doesn't fire twice for the same bar
  - Every signal goes through RiskManager before reaching the Executor
  - Every open/close is journaled to SQLite and (optionally) Telegram
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from src.execution.base import DataFeed, Executor, Order, OrderStatus
from src.execution.journal import TradeJournal
from src.monitoring.telegram_notifier import NoOpNotifier
from src.risk.position_sizing import lot_size_from_risk
from src.risk.risk_manager import RiskManager
from src.strategies.base import Signal, SignalType, Strategy

log = logging.getLogger(__name__)


@dataclass
class BotConfig:
    symbols: list[str]
    timeframe: str = "M15"
    bars_per_request: int = 500
    poll_interval_s: int = 60


@dataclass
class BotState:
    running: bool = False
    last_bar_ts: dict[tuple[str, str], pd.Timestamp] = field(default_factory=dict)
    last_heartbeat: datetime | None = None


class Bot:
    def __init__(
        self,
        config: BotConfig,
        strategies: dict[str, list[Strategy]],  # per-symbol list
        data_feed: DataFeed,
        executor: Executor,
        risk_manager: RiskManager,
        journal: TradeJournal,
        notifier=None,
    ) -> None:
        self.config = config
        self.strategies = strategies
        self.feed = data_feed
        self.executor = executor
        self.risk = risk_manager
        self.journal = journal
        self.notifier = notifier or NoOpNotifier()
        self.state = BotState()

    # ------------------------------------------------------------------ core

    def tick(self) -> int:
        """Run one pass across all symbols. Returns number of signals acted on."""
        self.state.last_heartbeat = datetime.now(timezone.utc)
        acted = 0

        for order in list(self.executor.open_orders()):
            if self._should_close_order(order):
                self._close_order(order, "stop/target")

        for symbol, strategies in self.strategies.items():
            if not strategies:
                continue
            bars = self.feed.latest_bars(symbol, self.config.timeframe, self.config.bars_per_request)
            if len(bars) < 10:
                continue

            bar_key = (symbol, self.config.timeframe)
            current_bar_ts = bars.index[-1]
            if self.state.last_bar_ts.get(bar_key) == current_bar_ts:
                continue
            self.state.last_bar_ts[bar_key] = current_bar_ts

            for strategy in strategies:
                signal = strategy.generate_signal(bars)
                if signal.type not in (SignalType.BUY, SignalType.SELL):
                    continue
                if self._handle_signal(signal, strategy):
                    acted += 1

        return acted

    def run_forever(self) -> None:
        self.state.running = True
        log.info("Bot loop starting — symbols=%s, tf=%s", self.config.symbols, self.config.timeframe)
        while self.state.running:
            try:
                acted = self.tick()
                if acted:
                    log.info("tick acted on %d signals", acted)
            except Exception:
                log.exception("unhandled error in tick")
            time.sleep(self.config.poll_interval_s)

    def stop(self) -> None:
        self.state.running = False

    # --------------------------------------------------------------- helpers

    def _handle_signal(self, signal: Signal, strategy: Strategy) -> bool:
        if signal.stop_loss is None or signal.take_profit is None:
            log.warning("signal from %s missing SL/TP — skipping", strategy.name)
            return False

        stop_distance_pips = abs(signal.price - signal.stop_loss) * 10_000
        decision = self.risk.evaluate(
            account_balance=self.executor.account_balance(),
            stop_distance_pips=stop_distance_pips,
            symbol=signal.symbol,
            lot_sizer=lot_size_from_risk,
        )
        if not decision.approved:
            log.info("risk rejected %s %s: %s", strategy.name, signal.symbol, decision.reason)
            return False

        order = Order(
            id=0,
            symbol=signal.symbol,
            side=signal.type,
            lot_size=decision.lot_size or 0.0,
            entry_price=signal.price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            opened_at=datetime.now(timezone.utc),
            strategy=strategy.name,
        )
        order = self.executor.place(order)
        if order.status == OrderStatus.REJECTED:
            log.warning("executor rejected order for %s", signal.symbol)
            return False

        self.journal.record_open(order)
        self.risk.register_trade_opened(self.risk.limits.risk_per_trade)
        self.notifier.trade_opened(
            symbol=order.symbol, side=order.side.value,
            lot_size=order.lot_size, price=order.entry_price,
        ) if hasattr(self.notifier, "trade_opened") else None

        log.info("OPENED %s %s %.2f lots @ %.5f (SL %.5f, TP %.5f) — %s",
                 order.side.value, order.symbol, order.lot_size,
                 order.entry_price, order.stop_loss, order.take_profit, strategy.name)
        return True

    def _should_close_order(self, order: Order) -> bool:
        bars = self.feed.latest_bars(order.symbol, self.config.timeframe, 2)
        if bars.empty:
            return False
        last = bars.iloc[-1]
        high, low = float(last["high"]), float(last["low"])
        if order.side == SignalType.BUY:
            return low <= order.stop_loss or high >= order.take_profit
        return high >= order.stop_loss or low <= order.take_profit

    def _close_order(self, order: Order, reason: str) -> None:
        closed = self.executor.close(order, reason)
        self.journal.record_close(closed)
        self.risk.register_trade_closed(self.risk.limits.risk_per_trade, closed.pnl)
        if hasattr(self.notifier, "trade_closed"):
            self.notifier.trade_closed(
                symbol=closed.symbol, side=closed.side.value,
                pnl=closed.pnl, reason=closed.close_reason,
            )
        log.info("CLOSED %s %s pnl=%+.2f reason=%s",
                 closed.side.value, closed.symbol, closed.pnl, closed.close_reason)
