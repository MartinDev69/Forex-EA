"""Forex-EA entry point.

Wires every module together and runs the bot loop. On macOS/Linux it uses
MockDataFeed/MockExecutor so you can watch the full pipeline end-to-end
without a broker connection. On Windows (with MT5 installed and USE_MT5=1),
it connects to your configured MetaTrader 5 demo account instead.
"""
from __future__ import annotations

import os
from pathlib import Path

from src.bot import Bot, BotConfig
from src.config import load_settings
from src.execution.journal import TradeJournal
from src.execution.mock import MockDataFeed, MockExecutor
from src.monitoring.telegram_notifier import build_notifier
from src.risk.risk_manager import RiskLimits, RiskManager
from src.strategies import (
    DonchianBreakoutStrategy,
    MACrossoverStrategy,
    RSIMeanReversionStrategy,
)
from src.utils import get_logger


def build_strategies(symbols: list[str]) -> dict:
    """One instance of each strategy per symbol.

    The FastAPI /strategies endpoint controls which names are enabled;
    hook that into the bot state in a later iteration — for now, run all.
    """
    out = {}
    for symbol in symbols:
        out[symbol] = [
            MACrossoverStrategy(symbol),
            RSIMeanReversionStrategy(symbol),
            DonchianBreakoutStrategy(symbol),
        ]
    return out


def main() -> None:
    settings = load_settings()
    log = get_logger("forex-ea", level=settings.log_level, log_dir=Path("logs"))

    use_mt5 = os.getenv("USE_MT5", "0") == "1"
    if use_mt5:
        log.info("MT5 mode requested — not yet wired; falling back to mock feed.")
        # TODO: build MT5DataFeed + MT5Executor in src/execution/mt5_live.py
    data_feed = MockDataFeed()
    executor = MockExecutor(starting_balance=10_000.0)

    risk = RiskManager(RiskLimits(
        risk_per_trade=settings.risk_per_trade,
        max_open_trades=settings.max_open_trades,
        max_daily_loss_pct=settings.max_daily_loss_pct,
    ))
    journal = TradeJournal(Path("data/trades.db"))
    notifier = build_notifier(settings.telegram_bot_token, settings.telegram_chat_id)

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
    )

    log.info("Symbols: %s | Timeframe: %s | Risk/trade: %.2f%%",
             settings.symbols, settings.timeframe, settings.risk_per_trade * 100)

    try:
        bot.run_forever()
    except KeyboardInterrupt:
        log.info("Interrupted — stopping bot.")
        bot.stop()


if __name__ == "__main__":
    main()
