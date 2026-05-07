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

from src.api.broker_status import BrokerStatusStore
from src.api.pending_orders import PendingOrder, PendingOrderStore
from src.allocator.allocator import ChampionChallengerAllocator
from src.allocator.score import score_pairs
from src.allocator.store import AllocationStore
from src.correlation.calculator import CorrelationCalculator
from src.correlation.store import CorrelationStore
from src.correlation.throttle import OpenPosition
from src.execution.base import DataFeed, Executor, Order, OrderStatus
from src.execution.fills import Fill, FillStore, signed_slippage_pips
from src.execution.journal import TradeJournal
from src.execution.stops import StopManager
from src.execution.strategy_toggles import StrategyToggleStore
from src.explanations.chart import serialise_bars, standard_overlays, strategy_decorations
from src.explanations.store import TradeExplanation, TradeExplanationStore
from src.ml.signal_filter import SignalFilter
from src.monitoring.telegram_notifier import NoOpNotifier
from src.narrator.composer import NarratorComposer
from src.regime.classifier import RegimeClassifier, RegimeSnapshot
from src.regime.store import RegimeStore
from src.replay.recorder import PathRecorder
from src.risk.position_sizing import lot_size_from_risk, pip_size
from src.risk.risk_manager import RiskManager
from src.strategies.base import Signal, SignalType, Strategy
from src.voice.killswitch import KillSwitchFlag
from src.watchdog.heartbeat import HeartbeatStore

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
    # Most recent regime snapshot per symbol — surfaced via the API so the
    # dashboard can show it without recomputing.
    last_regime: dict[str, RegimeSnapshot] = field(default_factory=dict)
    tick_count: int = 0
    last_correlation_refresh_tick: int = -1
    last_allocator_refresh_tick: int = -1
    # (strategy_name, symbol) -> (role, weight). Populated by the allocator.
    # Empty dict => allocator hasn't run yet, everything trades at 1.0.
    allocations: dict[tuple[str, str], tuple[str, float]] = field(default_factory=dict)
    # ISO date string of the last UTC day we sent a daily-summary Telegram.
    # Empty string = never sent — first eligible tick after the rollover hour
    # publishes the previous day's digest.
    last_daily_summary_date: str = ""
    # ISO year-week ('2026-W17') of the last weekly digest we shipped.
    last_weekly_digest_yw: str = ""
    # Calendar event keys ("isots:CCY:title") we've already announced as
    # "blackout incoming" so we don't re-spam the same event every tick.
    announced_blackouts: set[str] = field(default_factory=set)
    # (strategy, symbol) -> UTC datetime of the last setup-alert we sent.
    # Throttles "near miss" pings to one per pair-strategy per hour.
    last_setup_alert: dict[tuple[str, str], datetime] = field(default_factory=dict)
    # (strategy, symbol, side) -> last signal-mode alert timestamp; throttles
    # signal-only strategies so a persistent setup doesn't spam every tick.
    last_signal_alert: dict[tuple[str, str, str], datetime] = field(default_factory=dict)
    # Last tick on which the broker_status_store was refreshed with live
    # equity. -1 means we haven't pushed yet — first eligible tick will.
    last_broker_status_refresh_tick: int = -1


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
        toggle_store: StrategyToggleStore | None = None,
        signal_filter: SignalFilter | None = None,
        stop_manager: StopManager | None = None,
        regime_classifier: RegimeClassifier | None = None,
        regime_store: RegimeStore | None = None,
        correlation_calculator: CorrelationCalculator | None = None,
        correlation_store: CorrelationStore | None = None,
        correlation_refresh_ticks: int = 60,
        fill_store: FillStore | None = None,
        allocator: ChampionChallengerAllocator | None = None,
        allocation_store: AllocationStore | None = None,
        allocator_refresh_ticks: int = 60,
        allocator_score_window: int = 30,
        db_path: str = "data/trades.db",
        explanation_store: TradeExplanationStore | None = None,
        heartbeat_store: HeartbeatStore | None = None,
        heartbeat_process_name: str = "bot",
        narrator: NarratorComposer | None = None,
        path_recorder: PathRecorder | None = None,
        kill_switch_flag: KillSwitchFlag | None = None,
        broker_status_store: BrokerStatusStore | None = None,
        pending_orders_store: PendingOrderStore | None = None,
        broker_id: str = "",
        broker_status_refresh_ticks: int = 5,
    ) -> None:
        self.config = config
        self.strategies = strategies
        self.feed = data_feed
        self.executor = executor
        self.risk = risk_manager
        self.journal = journal
        self.notifier = notifier or NoOpNotifier()
        self.toggle_store = toggle_store
        self.signal_filter = signal_filter
        self.stop_manager = stop_manager
        # None = regime gating disabled; strategies fire regardless of regime.
        self.regime_classifier = regime_classifier
        # None = regime not persisted; the API /regime endpoint will report
        # "unknown" since it reads from the store, not from in-process state.
        self.regime_store = regime_store
        # Correlation calc + store work as a pair: we recompute the matrix
        # every `correlation_refresh_ticks` ticks and write it. The
        # PortfolioThrottle (held by RiskManager) reads from the same store.
        self.correlation_calculator = correlation_calculator
        self.correlation_store = correlation_store
        self.correlation_refresh_ticks = max(1, correlation_refresh_ticks)
        # None = no execution-quality logging. The hot path skips the write
        # entirely when this is unset, so the feature is zero-cost off.
        self.fill_store = fill_store
        # None = no allocator. Every (strategy, symbol) trades at full risk.
        # When set, the allocator scores variants from the journal and
        # publishes a per-pair weight that scales risk_per_trade per signal.
        self.allocator = allocator
        self.allocation_store = allocation_store
        self.allocator_refresh_ticks = max(1, allocator_refresh_ticks)
        self.allocator_score_window = max(5, allocator_score_window)
        self.db_path = db_path
        # None = "why this trade" panel won't have anything to show. Hot
        # path skips the INSERT entirely when off — zero cost when disabled.
        self.explanation_store = explanation_store
        # None = no heartbeat published. The external watchdog can't restart
        # a wedged bot it can't see, but the bot itself runs the same.
        self.heartbeat_store = heartbeat_store
        self.heartbeat_process_name = heartbeat_process_name
        # None = no LLM post-trade narration. When set, runs once per close
        # in best-effort mode — failures log but never crash the close path.
        self.narrator = narrator
        # None = no replay path captured. When set, records the bars a
        # closed trade traversed so /trades/{id}/replay can simulate
        # alternative SL/TP. Recorder swallows its own errors.
        self.path_recorder = path_recorder
        # None = voice kill switch off. When set, the bot polls the flag
        # at the top of each tick and halts cleanly if an operator has
        # tripped it from the API.
        self.kill_switch_flag = kill_switch_flag
        # None = the bot won't refresh broker_status periodically. main.py
        # passes a store + the broker id so the API can read live equity
        # without owning its own MT5 connection.
        self.broker_status_store = broker_status_store
        self.pending_orders_store = pending_orders_store
        self.broker_id = broker_id
        self.broker_status_refresh_ticks = max(1, broker_status_refresh_ticks)
        self.state = BotState()

    # ------------------------------------------------------------------ core

    def tick(self) -> int:
        """Run one pass across all symbols. Returns number of signals acted on."""
        self.state.last_heartbeat = datetime.now(timezone.utc)
        self.state.tick_count += 1
        acted = 0
        tick_error: str | None = None

        # Voice kill check first — if tripped, halt the loop before doing
        # anything else this tick. We don't auto-flatten open positions:
        # that's an operator decision (panic-close in volatile markets has
        # bitten people) and the kill itself is enough to prevent new entries.
        if self.kill_switch_flag is not None and self.kill_switch_flag.is_active():
            log.warning("voice kill switch active — halting bot loop")
            self._write_heartbeat("voice killswitch tripped")
            self.state.running = False
            return 0

        for order in list(self.executor.open_orders()):
            if self.stop_manager is not None:
                self._apply_trailing(order)
            if self._should_close_order(order):
                self._close_order(order, "stop/target")

        # Propfirm bookkeeping — must happen before signal evaluation so a
        # mid-tick DD breach kills new entries this same tick.
        if self.risk.propfirm_guard is not None:
            try:
                self.risk.propfirm_guard.observe(self.executor.account_balance())
            except Exception:
                log.exception("propfirm observe failed")

        self._maybe_refresh_correlations()
        self._maybe_refresh_allocator()
        self._maybe_refresh_broker_status()
        self._maybe_refresh_pending_orders()
        self._maybe_send_daily_summary()
        self._maybe_send_weekly_digest()
        self._maybe_warn_blackouts()

        for symbol, strategies in self.strategies.items():
            if not strategies:
                continue
            try:
                bars = self.feed.latest_bars(symbol, self.config.timeframe, self.config.bars_per_request)
            except Exception as exc:
                # One bad symbol shouldn't crash the whole tick — most often
                # this is a Market Watch / naming mismatch on a single pair.
                # Log it and move on so the rest of the symbols still trade.
                log.warning("feed failed for %s: %s", symbol, exc)
                continue
            if len(bars) < 10:
                continue

            bar_key = (symbol, self.config.timeframe)
            current_bar_ts = bars.index[-1]
            if self.state.last_bar_ts.get(bar_key) == current_bar_ts:
                continue
            self.state.last_bar_ts[bar_key] = current_bar_ts

            regime: RegimeSnapshot | None = None
            if self.regime_classifier is not None:
                regime = self.regime_classifier.classify(bars)
                self.state.last_regime[symbol] = regime
                if self.regime_store is not None:
                    try:
                        self.regime_store.upsert(symbol, regime)
                    except Exception:
                        log.exception("regime store upsert failed for %s", symbol)

            for strategy in strategies:
                if self.toggle_store is not None and not self.toggle_store.is_enabled(strategy.name):
                    continue
                if regime is not None and not self._regime_allows(strategy, regime):
                    log.info(
                        "regime gate rejected %s %s: trend=%s (prefers %s)",
                        strategy.name, symbol, regime.trend.value,
                        sorted(strategy.preferred_regimes),
                    )
                    continue
                signal = strategy.generate_signal(bars)
                if signal.type not in (SignalType.BUY, SignalType.SELL):
                    continue
                if self.signal_filter is not None and not self.signal_filter.should_take(
                    signal, bars, strategy.name
                ):
                    log.info("ML filter rejected %s %s", strategy.name, signal.symbol)
                    self._maybe_send_setup_alert(
                        strategy_name=strategy.name,
                        symbol=signal.symbol,
                        side=signal.type.value,
                        gate="ML filter",
                        detail=signal.reason or "below confidence threshold",
                        price=signal.price,
                    )
                    continue
                if self._handle_signal(signal, strategy, regime, bars=bars):
                    acted += 1

        self._write_heartbeat(tick_error)
        return acted

    def run_forever(self) -> None:
        self.state.running = True
        log.info("Bot loop starting — symbols=%s, tf=%s", self.config.symbols, self.config.timeframe)
        while self.state.running:
            try:
                acted = self.tick()
                if acted:
                    log.info("tick acted on %d signals", acted)
            except Exception as exc:
                log.exception("unhandled error in tick")
                # tick() didn't reach its own heartbeat write — publish one
                # here so the watchdog can see the bot is alive but erroring.
                self._write_heartbeat(repr(exc))
            time.sleep(self.config.poll_interval_s)

    def _write_heartbeat(self, last_error: str | None) -> None:
        if self.heartbeat_store is None:
            return
        try:
            self.heartbeat_store.write(
                process_name=self.heartbeat_process_name,
                tick_count=self.state.tick_count,
                last_error=last_error,
            )
        except Exception:
            log.exception("heartbeat write failed")

    def stop(self) -> None:
        self.state.running = False

    # --------------------------------------------------------------- helpers

    def _maybe_refresh_correlations(self) -> None:
        if self.correlation_calculator is None or self.correlation_store is None:
            return
        last = self.state.last_correlation_refresh_tick
        # Refresh on tick #1 too, so the first signal gets a populated store.
        if last >= 0 and (self.state.tick_count - last) < self.correlation_refresh_ticks:
            return

        cfg = self.correlation_calculator.config
        closes: dict[str, pd.Series] = {}
        for symbol in self.strategies.keys():
            try:
                bars = self.feed.latest_bars(symbol, self.config.timeframe, cfg.window_bars + 5)
            except Exception:
                log.exception("correlation refresh: feed failed for %s", symbol)
                continue
            if len(bars) >= cfg.min_observations + 1:
                closes[symbol] = bars["close"]

        if len(closes) < 2:
            self.state.last_correlation_refresh_tick = self.state.tick_count
            return

        try:
            matrix = self.correlation_calculator.matrix(closes)
            wrote = self.correlation_store.upsert_matrix(matrix, cfg.window_bars)
            log.info("correlation refresh: wrote %d pairs across %d symbols",
                     wrote, len(closes))
        except Exception:
            log.exception("correlation refresh failed — will retry next cycle")
        finally:
            self.state.last_correlation_refresh_tick = self.state.tick_count

    def _maybe_send_daily_summary(self) -> None:
        """Fire one daily_summary Telegram per UTC day, after 21:00 UTC.

        Picked 21:00 UTC because that's after the NY close — the trading day
        is effectively done. We dedupe by date so a restart inside the same
        day doesn't re-send.
        """
        if not hasattr(self.notifier, "daily_summary"):
            return
        now = datetime.now(timezone.utc)
        today_iso = now.date().isoformat()
        if self.state.last_daily_summary_date == today_iso:
            return
        if now.hour < 21:
            return
        try:
            today = self.journal.summary_today() if hasattr(self.journal, "summary_today") else {}
            balance = float(self.executor.account_balance()) if hasattr(self.executor, "account_balance") else 0.0
            self.notifier.daily_summary(
                trades=today.get("total", 0),
                wins=today.get("wins", 0),
                pnl=today.get("pnl", 0.0),
                equity=balance,
            )
            self.state.last_daily_summary_date = today_iso
            log.info("daily summary sent for %s", today_iso)
        except Exception:
            log.exception("daily summary send failed (will retry next tick)")

    def _maybe_refresh_broker_status(self) -> None:
        """Push the executor's live account snapshot into broker_status_store
        every N ticks so the API can show fresh equity + floating P&L
        without holding its own MT5 connection (only one process can attach
        to a given MT5 session at a time).
        """
        if self.broker_status_store is None:
            return
        last = self.state.last_broker_status_refresh_tick
        if last >= 0 and (self.state.tick_count - last) < self.broker_status_refresh_ticks:
            return
        try:
            info = self.executor.account_info() if hasattr(self.executor, "account_info") else None
        except Exception:
            log.exception("broker_status refresh: account_info failed")
            self.state.last_broker_status_refresh_tick = self.state.tick_count
            return
        if not info:
            self.state.last_broker_status_refresh_tick = self.state.tick_count
            return
        # Read the existing row so we don't blow away server/login that
        # main.py wrote at startup — write() does a full upsert.
        try:
            existing = self.broker_status_store.read()
        except Exception:
            existing = None
        try:
            self.broker_status_store.write(
                connected=True,
                broker=self.broker_id or (existing.broker if existing else None),
                server=existing.server if existing else None,
                login=existing.login if existing else None,
                account_info=info,
            )
        except Exception:
            log.exception("broker_status refresh: store write failed")
        finally:
            self.state.last_broker_status_refresh_tick = self.state.tick_count

    def _maybe_refresh_pending_orders(self) -> None:
        """Snapshot MT5's pending orders into the SQLite store so the
        API can list buy_limit/sell_limit/buy_stop/sell_stop without its
        own MT5 connection. Cheap call; runs every tick.
        """
        if self.pending_orders_store is None:
            return
        list_pending = getattr(self.executor, "list_pending_orders", None)
        if list_pending is None:
            return
        try:
            raw = list_pending() or []
        except Exception:
            log.exception("pending_orders refresh: list_pending_orders failed")
            return
        try:
            self.pending_orders_store.replace_all([
                PendingOrder(
                    ticket=int(o["ticket"]),
                    symbol=str(o["symbol"]),
                    order_type=str(o.get("order_type", "unknown")),
                    price=float(o["price"]),
                    volume=float(o["volume"]),
                    sl=float(o["sl"]) if o.get("sl") else None,
                    tp=float(o["tp"]) if o.get("tp") else None,
                    comment=o.get("comment"),
                    placed_at=o["placed_at"],
                )
                for o in raw
            ])
        except Exception:
            log.exception("pending_orders refresh: store write failed")

    def _maybe_send_weekly_digest(self) -> None:
        """One weekly_digest per ISO week, fired Sunday after 21:00 UTC.

        Same dedup pattern as the daily — we record the ISO year-week we've
        already shipped and skip until that rolls forward.
        """
        if not hasattr(self.notifier, "weekly_digest"):
            return
        now = datetime.now(timezone.utc)
        # weekday() Sun=6, Sat=5. We fire on Sun >= 21:00 UTC.
        if now.weekday() != 6 or now.hour < 21:
            return
        iso_yw = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
        if self.state.last_weekly_digest_yw == iso_yw:
            return
        try:
            window = (
                self.journal.summary_window(7)
                if hasattr(self.journal, "summary_window") else {}
            )
            balance = (
                float(self.executor.account_balance())
                if hasattr(self.executor, "account_balance") else 0.0
            )
            self.notifier.weekly_digest(
                trades=window.get("total", 0),
                wins=window.get("wins", 0),
                pnl=window.get("pnl", 0.0),
                equity=balance,
                best_symbol=window.get("best_symbol"),
                worst_symbol=window.get("worst_symbol"),
                best_strategy=window.get("best_strategy"),
            )
            self.state.last_weekly_digest_yw = iso_yw
            log.info("weekly digest sent for %s", iso_yw)
        except Exception:
            log.exception("weekly digest send failed (will retry next tick)")

    def _maybe_warn_blackouts(self) -> None:
        """Telegram a heads-up when a high-impact event is approaching.

        We fire 5 minutes before the actual blackout window starts (so the
        operator gets a chance to react before new entries are paused). The
        risk manager still blocks entries via current_blackout(); this is
        purely a notification layer.
        """
        if not hasattr(self.notifier, "blackout_warning"):
            return
        checker = getattr(self.risk, "blackout_checker", None)
        if checker is None or not checker.policy.enabled:
            return
        now = datetime.now(timezone.utc)
        # Warn `before_min + 5` minutes before the event — gives a lead time
        # before the actual blackout kicks in, AFTER which it's too late to
        # be useful as a "heads up".
        lead_window_min = checker.policy.before_min + 5
        # Map currency → list of symbols in our universe affected by it.
        # Build it once per call from the configured symbol list.
        from src.econ_calendar.symbols import currencies_for_symbol
        ccy_to_pairs: dict[str, list[str]] = {}
        for sym in self.strategies.keys():
            for ccy in currencies_for_symbol(sym):
                ccy_to_pairs.setdefault(ccy, []).append(sym)

        for sym in self.strategies.keys():
            event = checker.next_event(sym, now=now)
            if event is None:
                continue
            mins = event.minutes_until(now)
            if mins <= 0 or mins > lead_window_min:
                continue
            key = f"{event.event_time.isoformat()}:{event.currency}:{event.title}"
            if key in self.state.announced_blackouts:
                continue
            self.state.announced_blackouts.add(key)
            affected = sorted(set(ccy_to_pairs.get(event.currency, [])))
            try:
                self.notifier.blackout_warning(
                    title=event.title,
                    currency=event.currency,
                    minutes_until=mins,
                    affected_pairs=affected,
                    before_min=checker.policy.before_min,
                    after_min=checker.policy.after_min,
                )
                log.info("blackout warning sent for %s (%s)", event.title, event.currency)
            except Exception:
                log.exception("blackout warning send failed for %s", event.title)
            # Don't bombard — one warning per tick is enough; the next tick
            # will catch any other event that needs announcing.
            return

    def _strategy_mode(self, name: str) -> str:
        """'execute' | 'signal'. Defaults to 'execute' if the toggle store
        isn't wired or the strategy doesn't have a row yet.
        """
        if self.toggle_store is None or not hasattr(self.toggle_store, "get_mode"):
            return "execute"
        try:
            return self.toggle_store.get_mode(name)
        except Exception:
            log.exception("get_mode failed for %s", name)
            return "execute"

    def _send_signal_alert(
        self, signal: Signal, strategy: Strategy, regime: RegimeSnapshot | None = None,
    ) -> None:
        """Telegram-only signal for strategies in signal mode. Throttled
        per (strategy, symbol, side) so a persistent setup across an M15
        candle doesn't spam the chat.
        """
        if not hasattr(self.notifier, "signal_alert"):
            return
        key = (strategy.name, signal.symbol, signal.type.value)
        now = datetime.now(timezone.utc)
        last = self.state.last_signal_alert.get(key)
        if last is not None and (now - last).total_seconds() < 1800:  # 30 min
            return
        self.state.last_signal_alert[key] = now
        sl_pips = tp_pips = rr = None
        if signal.stop_loss is not None and signal.take_profit is not None:
            ps = pip_size(signal.symbol)
            if ps > 0:
                sl_pips = abs(signal.price - signal.stop_loss) / ps
                tp_pips = abs(signal.take_profit - signal.price) / ps
                if sl_pips > 0:
                    rr = tp_pips / sl_pips
        regime_label = regime.label if regime is not None else None
        try:
            self.notifier.signal_alert(
                symbol=signal.symbol,
                side=signal.type.value,
                strategy=strategy.name,
                price=signal.price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                sl_pips=sl_pips,
                tp_pips=tp_pips,
                risk_reward=rr,
                regime=regime_label,
                reason=signal.reason,
                indicators=signal.indicators,
            )
        except Exception:
            log.exception("signal alert send failed for %s %s", strategy.name, signal.symbol)

    def _maybe_send_setup_alert(
        self,
        *,
        strategy_name: str,
        symbol: str,
        side: str,
        gate: str,
        detail: str,
        price: float | None = None,
    ) -> None:
        """Fire a 'setup spotted but gated' Telegram, throttled to one per
        (strategy, symbol) per hour. The notifier additionally throttles
        per (symbol, side, gate-key) — together they avoid spamming the
        chat when the same heat-cap gate fires on every tick.
        """
        if not hasattr(self.notifier, "setup_alert"):
            return
        now = datetime.now(timezone.utc)
        last = self.state.last_setup_alert.get((strategy_name, symbol))
        if last is not None and (now - last).total_seconds() < 3600:
            return
        self.state.last_setup_alert[(strategy_name, symbol)] = now
        try:
            self.notifier.setup_alert(
                symbol=symbol,
                side=side,
                strategy=strategy_name,
                gate=gate,
                detail=detail,
                price=price,
            )
        except Exception:
            log.exception("setup alert send failed for %s %s", strategy_name, symbol)

    def _maybe_refresh_allocator(self) -> None:
        """Recompute per-(strategy, symbol) risk weights from recent trades.

        Runs on tick cadence (default every 60 ticks). One indexed query per
        active pair — bounded and cheap. If the allocator isn't wired, this
        is a no-op and the bot trades every variant at full risk.
        """
        if self.allocator is None:
            return
        last = self.state.last_allocator_refresh_tick
        if last >= 0 and (self.state.tick_count - last) < self.allocator_refresh_ticks:
            return

        pairs: list[tuple[str, str]] = []
        for symbol, strategies in self.strategies.items():
            for strategy in strategies:
                pairs.append((strategy.name, symbol))
        if not pairs:
            self.state.last_allocator_refresh_tick = self.state.tick_count
            return

        try:
            scores = score_pairs(self.db_path, pairs, window=self.allocator_score_window)
            allocations = self.allocator.allocate(scores)
            self.state.allocations = {
                (a.strategy, a.symbol): (a.role, a.weight) for a in allocations
            }
            if self.allocation_store is not None:
                self.allocation_store.upsert_many(allocations)
            log.info("allocator refresh: %d pairs scored, %d at full weight",
                     len(allocations),
                     sum(1 for a in allocations if a.weight >= 1.0))
        except Exception:
            log.exception("allocator refresh failed — keeping previous weights")
        finally:
            self.state.last_allocator_refresh_tick = self.state.tick_count

    def _open_positions_snapshot(self) -> list[OpenPosition]:
        risk_pct = self.risk.limits.risk_per_trade
        out: list[OpenPosition] = []
        for o in self.executor.open_orders():
            out.append(OpenPosition(symbol=o.symbol, side=o.side.value, risk_pct=risk_pct))
        return out

    @staticmethod
    def _regime_allows(strategy: Strategy, regime: RegimeSnapshot) -> bool:
        # Unknown regime (not enough bars yet) is permissive — otherwise the bot
        # would never fire on a freshly-started feed.
        prefs = strategy.preferred_regimes
        if not prefs:
            return True
        if regime.trend.value == "unknown":
            return True
        return regime.trend.value in prefs

    def _handle_signal(
        self, signal: Signal, strategy: Strategy, regime: RegimeSnapshot | None = None,
        bars: pd.DataFrame | None = None,
    ) -> bool:
        if signal.stop_loss is None or signal.take_profit is None:
            log.warning("signal from %s missing SL/TP — skipping", strategy.name)
            return False

        # If the strategy is in signal-only mode, fire a Telegram alert
        # and skip placement. We pass the same SL/TP/RR data the user
        # would see if the bot were taking the trade so they can mirror
        # it manually.
        mode = self._strategy_mode(strategy.name)
        if mode == "signal":
            self._send_signal_alert(signal, strategy, regime)
            return False

        stop_distance_pips = abs(signal.price - signal.stop_loss) / pip_size(signal.symbol)
        # Default to full weight when the allocator hasn't decided yet (cold
        # start) or isn't wired at all — the system shouldn't go silent just
        # because it has no opinion yet.
        role, weight = self.state.allocations.get(
            (strategy.name, signal.symbol), ("unmanaged", 1.0)
        )
        decision = self.risk.evaluate(
            account_balance=self.executor.account_balance(),
            stop_distance_pips=stop_distance_pips,
            symbol=signal.symbol,
            lot_sizer=lot_size_from_risk,
            side=signal.type.value,
            open_positions=self._open_positions_snapshot(),
            risk_multiplier=weight,
        )
        if not decision.approved:
            log.info("risk rejected %s %s: %s", strategy.name, signal.symbol, decision.reason)
            self._maybe_send_setup_alert(
                strategy_name=strategy.name,
                symbol=signal.symbol,
                side=signal.type.value,
                gate="risk manager",
                detail=decision.reason or "rejected",
                price=signal.price,
            )
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
        # Remember the original SL so trailing stops can compute R from it
        # even after we move the active stop.
        order.extra["initial_stop_loss"] = signal.stop_loss
        # Stash the weight so the close path subtracts the same heat we
        # added — otherwise probe trades would over- or under-credit the
        # portfolio heat tracker.
        order.extra["risk_weight"] = weight
        requested_price = signal.price
        send_started = time.perf_counter()
        order = self.executor.place(order)
        latency_ms = (time.perf_counter() - send_started) * 1000.0
        self._record_fill(order, "OPEN", requested_price, latency_ms)
        if order.status == OrderStatus.REJECTED:
            log.warning("executor rejected order for %s", signal.symbol)
            return False

        self.journal.record_open(order)
        self._record_explanation(order, signal, strategy, regime, role, weight, bars=bars)
        self.risk.register_trade_opened(self.risk.limits.risk_per_trade * weight)
        if self.risk.propfirm_guard is not None:
            try:
                self.risk.propfirm_guard.note_trade_opened()
            except Exception:
                log.exception("propfirm note_trade_opened failed")
        if hasattr(self.notifier, "trade_opened"):
            sl_dist = abs(order.entry_price - order.stop_loss)
            tp_dist = abs(order.take_profit - order.entry_price)
            ps = pip_size(order.symbol)
            sl_pips = sl_dist / ps if ps else None
            tp_pips = tp_dist / ps if ps else None
            rr = (tp_dist / sl_dist) if sl_dist > 0 else None
            regime_text = None
            if regime is not None:
                vol = regime.volatility.value if regime.volatility else ""
                trend = regime.trend.value if regime.trend else ""
                regime_text = f"{trend} · vol {vol}".strip(" ·")
            try:
                self.notifier.trade_opened(
                    symbol=order.symbol,
                    side=order.side.value,
                    lot_size=order.lot_size,
                    price=order.entry_price,
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    sl_pips=sl_pips,
                    tp_pips=tp_pips,
                    risk_reward=rr,
                    strategy=strategy.name,
                    regime=regime_text,
                    reason=signal.reason,
                )
            except TypeError:
                # Older notifier on the path — fall back to the basic shape so
                # we don't crash the open path on a partial deploy.
                self.notifier.trade_opened(
                    symbol=order.symbol, side=order.side.value,
                    lot_size=order.lot_size, price=order.entry_price,
                )

        log.info("OPENED %s %s %.2f lots @ %.5f (SL %.5f, TP %.5f) — %s",
                 order.side.value, order.symbol, order.lot_size,
                 order.entry_price, order.stop_loss, order.take_profit, strategy.name)
        return True

    def _record_fill(
        self, order: Order, event: str, requested_price: float, latency_ms: float,
    ) -> None:
        """Persist a fill event for execution-quality tracking. No-op if
        the fill_store wasn't injected — keeps the hot path free.
        """
        if self.fill_store is None:
            return
        is_filled = order.status == OrderStatus.OPEN if event == "OPEN" \
            else order.status == OrderStatus.CLOSED
        filled_price = (
            order.entry_price if event == "OPEN" else order.exit_price
        ) if is_filled else None
        slip = (
            signed_slippage_pips(order.symbol, order.side.value, requested_price, filled_price)
            if (is_filled and filled_price is not None) else None
        )
        try:
            self.fill_store.record(Fill(
                trade_id=order.id or None,
                symbol=order.symbol,
                side=order.side.value,
                event=event,  # type: ignore[arg-type]
                requested_price=requested_price,
                filled_price=filled_price,
                slippage_pips=slip,
                latency_ms=latency_ms,
                broker_ticket=order.broker_ticket,
                status="FILLED" if is_filled else "REJECTED",
                reason=order.close_reason or None,
                filled_at=datetime.now(timezone.utc),
            ))
        except Exception:
            # Logging shouldn't ever block trading. Swallow + log so a bad
            # disk write can't take down the bot loop.
            log.exception("fill_store.record failed for %s %s", event, order.symbol)

    def _record_explanation(
        self,
        order: Order,
        signal: Signal,
        strategy: Strategy,
        regime: RegimeSnapshot | None,
        allocator_role: str,
        allocator_weight: float,
        bars: pd.DataFrame | None = None,
    ) -> None:
        """Persist the decision context for /trades/{id}/explain. No-op if
        the explanation_store wasn't injected — zero cost when off.
        """
        if self.explanation_store is None:
            return
        if signal.stop_loss is None or signal.take_profit is None:
            # Defensive: _handle_signal already filters this out, but if a
            # caller invokes us directly we don't want to crash.
            return
        sl_dist = abs(signal.price - signal.stop_loss)
        tp_dist = abs(signal.take_profit - signal.price)
        rr = (tp_dist / sl_dist) if sl_dist > 0 else 0.0
        try:
            self.explanation_store.record(TradeExplanation(
                trade_id=order.id,
                strategy=strategy.name,
                symbol=order.symbol,
                side=order.side.value,
                signal_price=signal.price,
                signal_stop=signal.stop_loss,
                signal_target=signal.take_profit,
                risk_reward=rr,
                stop_distance_pips=abs(signal.price - signal.stop_loss) / pip_size(order.symbol),
                lot_size=order.lot_size,
                account_balance=self.executor.account_balance(),
                opened_at=order.opened_at.isoformat(),
                regime_trend=(regime.trend.value if regime is not None else None),
                regime_volatility=(regime.volatility.value if regime is not None else None),
                regime_label=(regime.label if regime is not None else None),
                regime_adx=(regime.adx if regime is not None else None),
                regime_atr_pct=(regime.atr_pct if regime is not None else None),
                # 'unmanaged' = allocator off; we want None on the wire so
                # the UI doesn't show a misleading role pill.
                allocator_role=(allocator_role if allocator_role != "unmanaged" else None),
                allocator_weight=(allocator_weight if allocator_role != "unmanaged" else None),
                # If the filter were rejecting, we'd have short-circuited
                # before this. None = no filter wired at all.
                ml_filter_passed=(True if self.signal_filter is not None else None),
                notes=signal.reason or "",
                indicators=dict(signal.indicators or {}),
                bars=serialise_bars(bars) if bars is not None else [],
                overlays=(
                    standard_overlays(bars) + strategy_decorations(strategy.name, bars)[0]
                    if bars is not None else []
                ),
                subplots=(
                    strategy_decorations(strategy.name, bars)[1]
                    if bars is not None else []
                ),
            ))
        except Exception:
            log.exception("explanation_store.record failed for trade %s", order.id)

    def _apply_trailing(self, order: Order) -> None:
        bars = self.feed.latest_bars(order.symbol, self.config.timeframe, 2)
        if bars.empty:
            return
        last = bars.iloc[-1]
        self.stop_manager.update_peak(order, float(last["high"]), float(last["low"]))
        new_sl = self.stop_manager.proposed_stop(order)
        if new_sl is None:
            return
        prev = order.stop_loss
        self.executor.modify(order, stop_loss=new_sl)
        log.info("TRAIL %s %s SL %.5f → %.5f",
                 order.side.value, order.symbol, prev, new_sl)

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
        # The "requested" close price is the level that triggered the exit:
        # take-profit for a target hit, stop-loss for everything else (timed
        # exit, trailing stop touch, manual). Slippage = filled vs that.
        requested_close = order.take_profit if reason == "target" else order.stop_loss
        send_started = time.perf_counter()
        closed = self.executor.close(order, reason)
        latency_ms = (time.perf_counter() - send_started) * 1000.0
        self._record_fill(closed, "CLOSE", requested_close, latency_ms)
        self.journal.record_close(closed)
        if self.path_recorder is not None and closed.closed_at is not None:
            try:
                self.path_recorder.record(
                    trade_id=closed.id,
                    symbol=closed.symbol,
                    opened_at=closed.opened_at,
                    closed_at=closed.closed_at,
                )
            except Exception:
                log.exception("path recorder failed for trade %d", closed.id)
        if self.narrator is not None:
            try:
                self.narrator.narrate(closed.id)
            except Exception:
                log.exception("narrator failed for trade %d", closed.id)
        # Pull the per-trade weight off the order so we credit back the
        # exact amount we charged on open (1.0 default if it wasn't set).
        weight = float(order.extra.get("risk_weight", 1.0)) if order.extra else 1.0
        self.risk.register_trade_closed(self.risk.limits.risk_per_trade * weight, closed.pnl)
        if hasattr(self.notifier, "trade_closed"):
            hold_minutes: float | None = None
            if closed.opened_at and closed.closed_at:
                hold_minutes = (closed.closed_at - closed.opened_at).total_seconds() / 60.0
            today = self.journal.summary_today() if hasattr(self.journal, "summary_today") else {}
            try:
                self.notifier.trade_closed(
                    symbol=closed.symbol,
                    side=closed.side.value,
                    pnl=closed.pnl,
                    reason=closed.close_reason,
                    exit_price=closed.exit_price,
                    hold_minutes=hold_minutes,
                    strategy=closed.strategy,
                    today_pnl=today.get("pnl"),
                    today_trades=today.get("total"),
                    today_wins=today.get("wins"),
                )
            except TypeError:
                self.notifier.trade_closed(
                    symbol=closed.symbol, side=closed.side.value,
                    pnl=closed.pnl, reason=closed.close_reason,
                )
        log.info("CLOSED %s %s pnl=%+.2f reason=%s",
                 closed.side.value, closed.symbol, closed.pnl, closed.close_reason)
