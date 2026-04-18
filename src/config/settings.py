from dataclasses import dataclass, field
from pathlib import Path
import os

from dotenv import load_dotenv


@dataclass
class Settings:
    mt5_login: int
    mt5_password: str
    mt5_server: str
    mt5_path: str

    symbols: list[str]
    timeframe: str
    risk_per_trade: float
    max_open_trades: int
    max_daily_loss_pct: float

    telegram_bot_token: str
    telegram_chat_id: str

    log_level: str = "INFO"
    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2])


def load_settings(env_file: str | Path | None = None) -> Settings:
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()

    return Settings(
        mt5_login=int(os.getenv("MT5_LOGIN", "0")),
        mt5_password=os.getenv("MT5_PASSWORD", ""),
        mt5_server=os.getenv("MT5_SERVER", ""),
        mt5_path=os.getenv("MT5_PATH", ""),
        symbols=[s.strip() for s in os.getenv("SYMBOLS", "EURUSD").split(",") if s.strip()],
        timeframe=os.getenv("TIMEFRAME", "M15"),
        risk_per_trade=float(os.getenv("RISK_PER_TRADE", "0.01")),
        max_open_trades=int(os.getenv("MAX_OPEN_TRADES", "3")),
        max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
