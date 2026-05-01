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
    def startup(
        self,
        *,
        broker: str,
        login: int,
        server: str,
        balance: float,
        currency: str,
        symbols: list[str],
        strategies: list[str],
        risk_pct: float,
        max_daily_loss_pct: float,
    ) -> bool:
        sym_str = ", ".join(symbols)
        strat_str = ", ".join(s.replace("_", " ").title() for s in strategies) or "none"
        return self.send(
            f"<b>🤖 AntiGreed online</b>\n"
            f"Broker: {broker} #{login} ({server})\n"
            f"Balance: {balance:.2f} {currency}\n"
            f"Pairs: <code>{sym_str}</code>\n"
            f"Strategies: {strat_str}\n"
            f"Risk: {risk_pct:.1%}/trade · Daily cap {max_daily_loss_pct:.0%}"
        )

    def trade_opened(
        self,
        *,
        symbol: str,
        side: str,
        lot_size: float,
        price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        sl_pips: float | None = None,
        tp_pips: float | None = None,
        risk_reward: float | None = None,
        strategy: str | None = None,
        regime: str | None = None,
        reason: str | None = None,
    ) -> bool:
        emoji = "🟢" if side.upper() == "BUY" else "🔴"
        lines = [f"<b>{emoji} {side.upper()} {symbol}</b>"]
        if strategy:
            lines.append(f"Strategy: {strategy.replace('_', ' ').title()}")
        lines.append(f"Entry: {price:.5f} · {lot_size:.2f} lots")
        if stop_loss is not None and take_profit is not None:
            sl_text = f"{stop_loss:.5f}"
            tp_text = f"{take_profit:.5f}"
            if sl_pips is not None:
                sl_text += f" ({sl_pips:.0f} pips)"
            if tp_pips is not None:
                tp_text += f" ({tp_pips:.0f} pips)"
            lines.append(f"SL: {sl_text}")
            lines.append(f"TP: {tp_text}")
        if risk_reward is not None:
            lines.append(f"R:R {risk_reward:.1f}")
        if regime:
            lines.append(f"Regime: {regime}")
        if reason:
            lines.append(f"<i>{reason}</i>")
        return self.send("\n".join(lines))

    def trade_closed(
        self,
        *,
        symbol: str,
        side: str,
        pnl: float,
        reason: str,
        exit_price: float | None = None,
        hold_minutes: float | None = None,
        strategy: str | None = None,
        today_pnl: float | None = None,
        today_trades: int | None = None,
        today_wins: int | None = None,
    ) -> bool:
        emoji = "✅" if pnl >= 0 else "❌"
        lines = [f"<b>{emoji} {pnl:+.2f} · {side.upper()} {symbol}</b>"]
        if strategy:
            lines.append(f"Strategy: {strategy.replace('_', ' ').title()}")
        if exit_price is not None:
            lines.append(f"Exit: {exit_price:.5f} ({reason})")
        else:
            lines.append(f"Reason: {reason}")
        if hold_minutes is not None:
            lines.append(f"Held: {self._fmt_duration(hold_minutes)}")
        if today_pnl is not None and today_trades is not None:
            wr = (today_wins / today_trades) if today_trades and today_wins is not None else 0
            wr_text = f" · {wr:.0%} WR" if today_wins is not None else ""
            lines.append(f"Today: {today_pnl:+.2f} ({today_trades}{wr_text})")
        return self.send("\n".join(lines))

    def daily_summary(
        self, *, trades: int, wins: int, pnl: float, equity: float,
        best_pair: str | None = None, worst_pair: str | None = None,
    ) -> bool:
        win_rate = wins / trades if trades else 0
        emoji = "📈" if pnl >= 0 else "📉"
        lines = [
            f"<b>{emoji} Daily Summary</b>",
            f"Trades: {trades} · Wins: {wins} ({win_rate:.0%})",
            f"P&L: {pnl:+.2f}",
            f"Equity: {equity:.2f}",
        ]
        if best_pair:
            lines.append(f"Best: {best_pair}")
        if worst_pair and worst_pair != best_pair:
            lines.append(f"Worst: {worst_pair}")
        return self.send("\n".join(lines))

    def blackout_warning(
        self,
        *,
        title: str,
        currency: str,
        minutes_until: float,
        affected_pairs: list[str],
        before_min: int,
        after_min: int,
    ) -> bool:
        pairs = ", ".join(affected_pairs) if affected_pairs else "—"
        return self.send(
            f"<b>⚠️ {title}</b> · <b>{currency}</b>\n"
            f"Event in {self._fmt_duration(minutes_until)} · "
            f"blackout {before_min}m before → {after_min}m after\n"
            f"New entries paused on: <code>{pairs}</code>"
        )

    def setup_alert(
        self,
        *,
        symbol: str,
        side: str,
        strategy: str,
        gate: str,
        detail: str,
    ) -> bool:
        emoji = "👀"
        return self.send(
            f"<b>{emoji} Setup spotted: {side.upper()} {symbol}</b>\n"
            f"Strategy: {strategy.replace('_', ' ').title()}\n"
            f"Gated by {gate}: <i>{detail}</i>"
        )

    def weekly_digest(
        self,
        *,
        trades: int,
        wins: int,
        pnl: float,
        equity: float,
        best_symbol: str | None,
        worst_symbol: str | None,
        best_strategy: str | None,
    ) -> bool:
        win_rate = wins / trades if trades else 0
        emoji = "🏆" if pnl >= 0 else "🧹"
        lines = [
            f"<b>{emoji} Weekly Digest</b>",
            f"Trades: {trades} · Wins: {wins} ({win_rate:.0%})",
            f"P&L: {pnl:+.2f}",
            f"Equity: {equity:.2f}",
        ]
        if best_strategy:
            lines.append(f"Top strategy: {best_strategy.replace('_', ' ').title()}")
        if best_symbol:
            lines.append(f"Best pair: {best_symbol}")
        if worst_symbol and worst_symbol != best_symbol:
            lines.append(f"Worst pair: {worst_symbol}")
        return self.send("\n".join(lines))

    @staticmethod
    def _fmt_duration(minutes: float) -> str:
        if minutes < 60:
            return f"{minutes:.0f} min"
        hours = minutes / 60
        if hours < 24:
            return f"{hours:.1f}h"
        return f"{hours / 24:.1f}d"


def build_notifier(bot_token: str | None, chat_id: str | None) -> Notifier:
    if bot_token and chat_id:
        return TelegramNotifier(bot_token, chat_id)
    log.info("Telegram disabled — no credentials configured")
    return NoOpNotifier()
