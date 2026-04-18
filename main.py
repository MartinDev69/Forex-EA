"""Entry point for the Forex-EA bot.

Week 1: prints config + attempts MT5 connection. Real trading loop lands in Week 5.
"""
from pathlib import Path

from src.config import load_settings
from src.utils import get_logger


def main() -> None:
    settings = load_settings()
    log = get_logger("forex-ea", level=settings.log_level, log_dir=Path("logs"))

    log.info("Forex-EA starting")
    log.info("Symbols: %s", settings.symbols)
    log.info("Timeframe: %s", settings.timeframe)
    log.info("Risk per trade: %.2f%%", settings.risk_per_trade * 100)

    try:
        from src.connection import MT5Client
        client = MT5Client(
            login=settings.mt5_login,
            password=settings.mt5_password,
            server=settings.mt5_server,
            path=settings.mt5_path or None,
        )
        with client:
            info = client.account_info()
            log.info(
                "Connected to %s | balance=%.2f %s | leverage=1:%d",
                info.server, info.balance, info.currency, info.leverage,
            )
    except RuntimeError as exc:
        log.warning("MT5 unavailable: %s", exc)
    except ConnectionError as exc:
        log.error("MT5 connection failed: %s", exc)


if __name__ == "__main__":
    main()
