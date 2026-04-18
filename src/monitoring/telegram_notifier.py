"""Telegram alerts for trade events, errors, and daily summaries.

Uses the raw Bot API via urllib so the robot can alert even when
python-telegram-bot isn't installed. The NoOpNotifier is a safe
default when no credentials are configured.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Protocol

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier(Protocol):
    def send(self, text: str) -> bool: ...


class NoOpNotifier:
    def send(self, text: str) -> bool:
        log.debug("notifier disabled, would send: %s", text)
        return True


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout_s: float = 10.0) -> None:
        if not bot_token or not chat_id:
            raise ValueError("bot_token and chat_id required")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout_s = timeout_s

    def send(self, text: str) -> bool:
        url = API_URL.format(token=self.bot_token)
        data = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode("utf-8")
        try:
            with urllib.request.urlopen(url, data=data, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                if not payload.get("ok"):
                    log.warning("telegram send failed: %s", payload)
                    return False
                return True
        except Exception as exc:
            log.exception("telegram error: %s", exc)
            return False

    # Convenience helpers -------------------------------------------------
    def trade_opened(self, symbol: str, side: str, lot_size: float, price: float) -> bool:
        return self.send(
            f"<b>🟢 {side} {symbol}</b>\n"
            f"Lots: {lot_size}\n"
            f"Price: {price}"
        )

    def trade_closed(self, symbol: str, side: str, pnl: float, reason: str) -> bool:
        emoji = "✅" if pnl >= 0 else "❌"
        return self.send(
            f"<b>{emoji} CLOSED {side} {symbol}</b>\n"
            f"P&L: {pnl:+.2f}\n"
            f"Reason: {reason}"
        )

    def daily_summary(self, trades: int, wins: int, pnl: float, equity: float) -> bool:
        win_rate = wins / trades if trades else 0
        return self.send(
            f"<b>📊 Daily Summary</b>\n"
            f"Trades: {trades} | Wins: {wins} ({win_rate:.0%})\n"
            f"P&L: {pnl:+.2f}\n"
            f"Equity: {equity:.2f}"
        )


def build_notifier(bot_token: str | None, chat_id: str | None) -> Notifier:
    if bot_token and chat_id:
        return TelegramNotifier(bot_token, chat_id)
    log.info("Telegram disabled — no credentials configured")
    return NoOpNotifier()
