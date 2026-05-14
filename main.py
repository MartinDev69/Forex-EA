"""Forex-EA entry point.

Wires every module together and runs the bot loop. On macOS/Linux it uses
MockDataFeed/MockExecutor so you can watch the full pipeline end-to-end
without a broker connection. On Windows (with MT5 installed and USE_MT5=1),
it connects to your configured MetaTrader 5 demo account instead.
"""
from __future__ import annotations

import os
from pathlib import Path

from src.api.broker_config import BrokerConfig, BrokerConfigStore
from src.api.broker_status import BrokerStatusStore
from src.api.pending_orders import PendingOrderStore
from src.execution.signal_dedup import SignalDedupStore
from src.bot import Bot, BotConfig
from src.config import load_settings
from src.connection.mt5_client import MT5Client
from src.correlation import (
    CorrelationCalculator,
    CorrelationConfig,
    CorrelationStore,
    PortfolioThrottle,
    ThrottlePolicy,
)
from src.execution.base import DataFeed, Executor
from src.allocator import AllocationStore, AllocatorPolicy, ChampionChallengerAllocator
from src.execution.fills import FillStore
from src.explanations import TradeExplanationStore
from src.execution.journal import TradeJournal
from src.execution.mock import MockDataFeed, MockExecutor
from src.execution.mt5_live import MT5DataFeed, MT5Executor
from src.execution.stops import StopManager, StopPolicy
from src.execution.strategy_toggles import DEFAULT_STRATEGY_FLAGS, StrategyToggleStore
from src.ml.signal_filter import SignalFilter
from src.monitoring.telegram_notifier import build_notifier
from src.regime import RegimeClassifier, RegimeConfig, RegimeStore
from src.risk.risk_manager import RiskLimits, RiskManager
from src.strategies import (
    STRATEGY_REGISTRY,
    ADXBreakoutStrategy,
    BollingerBounceStrategy,
    BollingerSqueezeStrategy,
    DonchianBreakoutStrategy,
    EMAPullbackStrategy,
    EngulfingPatternStrategy,
    InsideBarBreakoutStrategy,
    MACDCrossStrategy,
    MACrossoverStrategy,
    RSIMeanReversionStrategy,
    StochasticReversalStrategy,
    TripleMAStrategy,
)
from src.narrator import NarrativeStore, NarratorComposer, build_provider
from src.propfirm import PropFirmGuard, PropFirmStore, policy_from_env
from src.replay import PathRecorder, PathStore
from src.utils import get_logger
from src.voice import KillSwitchFlag
from src.api.bot_control import BotControlStore
from src.watchdog import HeartbeatStore


def build_strategies(symbols: list[str]) -> dict:
    """One instance of each strategy per symbol. The toggle store decides
    which actually fire and in what mode (execute vs signal).
    """
    out = {}
    for symbol in symbols:
        out[symbol] = [
            MACrossoverStrategy(symbol),
            RSIMeanReversionStrategy(symbol),
            DonchianBreakoutStrategy(symbol),
            MACDCrossStrategy(symbol),
            BollingerBounceStrategy(symbol),
            BollingerSqueezeStrategy(symbol),
            StochasticReversalStrategy(symbol),
            TripleMAStrategy(symbol),
            InsideBarBreakoutStrategy(symbol),
            EngulfingPatternStrategy(symbol),
            EMAPullbackStrategy(symbol),
            ADXBreakoutStrategy(symbol),
        ]
    return out


def _resolve_broker_config(settings) -> tuple[BrokerConfig, str]:
    """Prefer the DB-stored broker config (editable from the dashboard) over
    .env values. Returns (config, source) where source is 'dashboard' or 'env'.
    """
    auth_secret = os.getenv("AUTH_SECRET")
    if auth_secret:
        try:
            store = BrokerConfigStore(Path("data/trades.db"), secret=auth_secret)
            cfg = store.get_decrypted()
            if cfg is not None:
                return cfg, "dashboard"
        except Exception:
            # Fall through to env — if the DB config is broken, env is the safety net.
            pass
    return (
        BrokerConfig(
            broker="env",
            login=settings.mt5_login,
            password=settings.mt5_password,
            server=settings.mt5_server,
            mt5_path=settings.mt5_path or "",
        ),
        "env",
    )


def main() -> None:
    settings = load_settings()
    log = get_logger("forex-ea", level=settings.log_level, log_dir=Path("logs"))

    use_mt5 = os.getenv("USE_MT5", "0") == "1"
    data_feed: DataFeed
    executor: Executor
    mt5_client: MT5Client | None = None
    status_store = BrokerStatusStore(Path("data/trades.db"))
    pending_store = PendingOrderStore(Path("data/trades.db"))
    dedup_store = SignalDedupStore(Path("data/trades.db"))
    if use_mt5:
        broker_cfg, source = _resolve_broker_config(settings)
        log.info("Loading broker config from %s (broker=%s, login=%s, server=%s)",
                 source, broker_cfg.broker, broker_cfg.login, broker_cfg.server)
        try:
            mt5_client = MT5Client(
                login=broker_cfg.login,
                password=broker_cfg.password,
                server=broker_cfg.server,
                path=broker_cfg.mt5_path or None,
            )
            mt5_client.connect()
            info = mt5_client.account_info()
            log.info("MT5 connected: login=%s server=%s balance=%.2f %s",
                     info.login, info.server, info.balance, info.currency)
            # Hand the same MetaTrader5 module the client uses to the pip
            # resolver — pip_size/pip_value calls now query symbol_info on
            # the live terminal instead of guessing from the symbol name.
            # Critical for Deriv synthetics, indices, crypto — anything
            # outside the small hardcoded fallback table.
            try:
                import MetaTrader5 as _mt5  # noqa: PLC0415 — runtime import on Windows VPS
                from src.risk.position_sizing import PipResolver, set_resolver
                set_resolver(PipResolver(_mt5))
                log.info("Pip resolver installed — pip math now driven by MT5 symbol_info.")
            except Exception:
                log.exception("Could not install live pip resolver; falling back to hardcoded table.")
            status_store.write(
                connected=True,
                broker=broker_cfg.broker,
                server=info.server,
                login=info.login,
                account_info={
                    "balance": info.balance, "equity": info.equity,
                    "currency": info.currency, "leverage": info.leverage,
                },
            )
        except Exception as e:
            status_store.write(
                connected=False,
                broker=broker_cfg.broker,
                server=broker_cfg.server,
                login=broker_cfg.login,
                last_error=str(e),
            )
            raise
        data_feed = MT5DataFeed()
        executor = MT5Executor(symbols_filter=settings.symbols)
    else:
        data_feed = MockDataFeed()
        executor = MockExecutor(starting_balance=10_000.0)
        status_store.write(connected=False, last_error="USE_MT5=0 (mock mode)")

    correlation_calculator: CorrelationCalculator | None = None
    correlation_store: CorrelationStore | None = None
    portfolio_throttle: PortfolioThrottle | None = None
    throttle_policy = ThrottlePolicy.from_env()
    if throttle_policy.enabled:
        correlation_calculator = CorrelationCalculator(CorrelationConfig.from_env())
        correlation_store = CorrelationStore(Path("data/trades.db"))
        portfolio_throttle = PortfolioThrottle(correlation_store, policy=throttle_policy)
        log.info(
            "Correlation throttle enabled (max heat=%.2f%%, floor=%.2f, window=%d bars)",
            throttle_policy.max_correlated_heat_pct * 100,
            throttle_policy.correlation_floor,
            correlation_calculator.config.window_bars,
        )

    propfirm_guard: PropFirmGuard | None = None
    if os.getenv("PROPFIRM_ENABLED", "0").strip() not in ("0", "false", "False", ""):
        pf_policy = policy_from_env()
        pf_store = PropFirmStore(Path("data/trades.db"))
        propfirm_guard = PropFirmGuard(pf_policy, pf_store)
        log.info(
            "PropFirm guard enabled (preset=%s, daily=%.1f%%, total=%.1f%%, target=%.1f%%)",
            pf_policy.preset_name,
            pf_policy.max_daily_loss_pct * 100,
            pf_policy.max_total_drawdown_pct * 100,
            pf_policy.profit_target_pct * 100,
        )

    risk = RiskManager(
        RiskLimits(
            risk_per_trade=settings.risk_per_trade,
            max_open_trades=settings.max_open_trades,
            max_daily_loss_pct=settings.max_daily_loss_pct,
            max_portfolio_heat_pct=settings.max_portfolio_heat_pct,
        ),
        portfolio_throttle=portfolio_throttle,
        propfirm_guard=propfirm_guard,
    )
    log.info(
        "Risk: %.2f%%/trade · max %d open · daily-loss %.0f%% · portfolio-heat %.0f%%",
        settings.risk_per_trade * 100,
        settings.max_open_trades,
        settings.max_daily_loss_pct * 100,
        settings.max_portfolio_heat_pct * 100,
    )
    journal = TradeJournal(Path("data/trades.db"))
    toggle_store = StrategyToggleStore(Path("data/trades.db"))
    toggle_store.initialize_defaults({
        name: DEFAULT_STRATEGY_FLAGS.get(name, False) for name in STRATEGY_REGISTRY
    })

    # Telegram fan-out callback: every per-trade message lets the
    # notifier ask "who else should receive this for strategy X?" and
    # we return the chat IDs of active operators whose admin-marked
    # copyable strategies include X. None → no strategy info, only
    # admin gets it.
    from src.api.users import UserStore
    user_store_for_notifier = UserStore(Path("data/trades.db"))

    def _telegram_recipients(strategy: str | None, kind: str) -> list[int | str]:
        """Return the chat IDs of operators who should also receive this
        per-trade message. Two filters:
          1. Admin's user_copyable flag — admin-only strategies don't
             fan out to anyone but admin.
          2. The operator's own picks for the matching kind — only the
             3 signal strategies and 2 execute strategies they chose at
             signup.
        """
        if strategy is None:
            return []  # non-trade message — admin-only by design
        try:
            if not toggle_store.is_user_copyable(strategy):
                return []
            chats: list[int | str] = []
            for _, cid in user_store_for_notifier.list_users_who_picked(
                strategy, kind,
            ):
                if cid is not None:
                    chats.append(int(cid))
            return chats
        except Exception:
            log.exception("telegram recipients lookup failed for %s/%s",
                          strategy, kind)
            return []

    notifier = build_notifier(
        settings.telegram_bot_token,
        settings.telegram_chat_id,
        recipients_for_strategy=_telegram_recipients,
    )
    if hasattr(notifier, "startup") and use_mt5 and mt5_client is not None:
        # Best-effort online ping. The notifier swallows network errors so
        # this can never block the bot from starting.
        try:
            enabled_strategies = [
                name for name, on in toggle_store.list().items() if on
            ]
            notifier.startup(
                broker=broker_cfg.broker,
                login=info.login,
                server=info.server,
                balance=info.balance,
                currency=info.currency,
                symbols=settings.symbols,
                strategies=enabled_strategies,
                risk_pct=settings.risk_per_trade,
                max_daily_loss_pct=settings.max_daily_loss_pct,
            )
        except Exception:
            log.exception("startup notification failed (continuing)")

    signal_filter: SignalFilter | None = None
    model_path = Path(os.getenv("ML_MODEL_PATH", "data/models/signal_filter.json"))
    if model_path.exists():
        threshold = float(os.getenv("ML_THRESHOLD", "0.55"))
        signal_filter = SignalFilter.load(model_path, threshold=threshold)
        log.info("ML signal filter loaded from %s (threshold=%.2f)", model_path, threshold)
    else:
        log.info("No ML model at %s — signals run unfiltered.", model_path)

    regime_classifier: RegimeClassifier | None = None
    regime_store: RegimeStore | None = None
    if os.getenv("REGIME_ENABLED", "1").strip() not in ("0", "false", "False", ""):
        regime_classifier = RegimeClassifier(RegimeConfig.from_env())
        regime_store = RegimeStore(Path("data/trades.db"))
        log.info(
            "Regime classifier enabled (ADX>=%.0f -> trend). Strategies with "
            "preferred_regimes not matching the current regime will be gated.",
            regime_classifier.config.adx_trend_threshold,
        )

    bot = Bot(
        config=BotConfig(
            symbols=settings.symbols,
            timeframe=settings.timeframe,
            poll_interval_s=10,  # tight poll for mock; raise to 60 on live
        ),
        strategies=build_strategies(settings.symbols),
        data_feed=data_feed,
        executor=executor,
        risk_manager=risk,
        journal=journal,
        notifier=notifier,
        toggle_store=toggle_store,
        signal_filter=signal_filter,
        stop_manager=StopManager(StopPolicy()),
        regime_classifier=regime_classifier,
        regime_store=regime_store,
        correlation_calculator=correlation_calculator,
        correlation_store=correlation_store,
        correlation_refresh_ticks=int(os.getenv("CORRELATION_REFRESH_TICKS", "60")),
        fill_store=(
            FillStore(Path("data/trades.db"))
            if os.getenv("EXEC_QUALITY_ENABLED", "1").strip() not in ("0", "false", "False", "")
            else None
        ),
        # Allocator is opt-in. When off, every (strategy, symbol) trades at
        # full risk and the hot path skips the refresh entirely.
        allocator=(
            ChampionChallengerAllocator(AllocatorPolicy.from_env())
            if os.getenv("ALLOCATOR_ENABLED", "0").strip() not in ("0", "false", "False", "")
            else None
        ),
        allocation_store=(
            AllocationStore(Path("data/trades.db"))
            if os.getenv("ALLOCATOR_ENABLED", "0").strip() not in ("0", "false", "False", "")
            else None
        ),
        allocator_refresh_ticks=int(os.getenv("ALLOCATOR_REFRESH_TICKS", "60")),
        allocator_score_window=int(os.getenv("ALLOCATOR_SCORE_WINDOW", "30")),
        db_path="data/trades.db",
        # On by default — one tiny INSERT per trade-open powers the
        # "why this trade?" panel. Set EXPLANATIONS_ENABLED=0 to skip.
        explanation_store=(
            TradeExplanationStore(Path("data/trades.db"))
            if os.getenv("EXPLANATIONS_ENABLED", "1").strip() not in ("0", "false", "False", "")
            else None
        ),
        # On by default. One UPSERT per tick lets the external watchdog
        # detect a wedged bot. Set WATCHDOG_ENABLED=0 to skip the writes
        # (the watchdog will then have no heartbeat to read and won't act).
        heartbeat_store=(
            HeartbeatStore(Path("data/trades.db"))
            if os.getenv("WATCHDOG_ENABLED", "1").strip() not in ("0", "false", "False", "")
            else None
        ),
        # Off by default. When on, runs once per close to write a 2-3 sentence
        # post-mortem to the narratives table. Network latency only hits the
        # close path; the call is wrapped in try/except so failures never
        # propagate. Provider falls back to stub when no API key is set.
        narrator=(
            NarratorComposer(
                provider=build_provider(),
                store=NarrativeStore(Path("data/trades.db")),
                db_path="data/trades.db",
            )
            if os.getenv("NARRATOR_ENABLED", "0").strip() not in ("0", "false", "False", "")
            else None
        ),
        # Off by default. Captures OHLC bars over the trade's lifecycle so
        # /trades/{id}/replay can walk them with tweaked SL/TP. One small
        # batch INSERT per close — recorder swallows feed errors.
        path_recorder=(
            PathRecorder(
                feed=data_feed,
                store=PathStore(Path("data/trades.db")),
                timeframe=settings.timeframe,
            )
            if os.getenv("REPLAY_ENABLED", "0").strip() not in ("0", "false", "False", "")
            else None
        ),
        # Off by default. When on, the bot polls a SQLite flag at the top
        # of each tick and halts cleanly if an operator has tripped the
        # kill switch via /voice/command. One tiny SELECT per tick when on.
        kill_switch_flag=(
            KillSwitchFlag(Path("data/trades.db"))
            if os.getenv("VOICE_KILLSWITCH_ENABLED", "0").strip() not in ("0", "false", "False", "")
            else None
        ),
        # Cross-process Start/Stop. The API writes this when an operator
        # clicks the toggle in the dashboard; the bot polls it each tick.
        # Always on — operators expect Stop to mean stop.
        bot_control_store=BotControlStore(Path("data/trades.db")),
        # Tick refreshes broker_status_store with live equity + floating P&L
        # so the API can compute Today P&L (realized + open) without owning
        # its own MT5 connection (only one Python process can attach).
        broker_status_store=status_store if use_mt5 else None,
        pending_orders_store=pending_store if use_mt5 else None,
        signal_dedup_store=dedup_store,
        broker_id=broker_cfg.broker if use_mt5 else "",
    )

    log.info("Strategy toggles: %s", toggle_store.list())

    log.info("Symbols: %s | Timeframe: %s | Risk/trade: %.2f%%",
             settings.symbols, settings.timeframe, settings.risk_per_trade * 100)

    try:
        bot.run_forever()
    except KeyboardInterrupt:
        log.info("Interrupted — stopping bot.")
        bot.stop()
    finally:
        if mt5_client is not None:
            mt5_client.disconnect()
            status_store.write(connected=False, last_error="bot shut down")


if __name__ == "__main__":
    main()
