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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"

# Emoji glossary — kept consistent so a glance distinguishes
# operational alerts from trade events from informational chatter.
EMOJI = {
    "online":   "🤖",
    "offline":  "💤",
    "buy":      "🟢",
    "sell":     "🔴",
    "win":      "✅",
    "loss":     "❌",
    "scratch":  "➖",
    "heat":     "🛡️",   # risk-manager / heat-cap gates
    "blackout": "🚫",   # calendar / news blackouts
    "regime":   "🌫️",   # regime mismatch
    "cooldown": "⏳",   # cooldown / re-entry locks
    "calendar": "📅",
    "warn":     "⚠️",
    "win_day":  "📈",
    "loss_day": "📉",
    "trophy":   "🏆",
    "broom":    "🧹",
}

# Gates with a known severity get distinct icons. Anything we don't
# recognise falls back to 🛡️ so the message is still readable.
GATE_ICONS = {
    "risk manager":     EMOJI["heat"],
    "portfolio heat":   EMOJI["heat"],
    "calendar":         EMOJI["blackout"],
    "blackout":         EMOJI["blackout"],
    "regime":           EMOJI["regime"],
    "cooldown":         EMOJI["cooldown"],
    "kill switch":      "🛑",
}


def _decimals_for(symbol: str) -> int:
    """Pick a sensible decimal count for the given instrument so a EURUSD
    price doesn't show as 1.16800 next to a gold price as 4559.36000.
    """
    s = (symbol or "").upper()
    if "XAU" in s or "GOLD" in s:
        return 2
    if "OIL" in s or "WTI" in s or "BRENT" in s:
        return 2
    if s.endswith("JPY") or s.endswith("JPYM"):
        return 3
    if "BTC" in s or "ETH" in s:
        return 1
    if any(idx in s for idx in ("US30", "US500", "NAS", "GER", "UK100", "JP225")):
        return 1
    return 5


def _fmt_price(symbol: str, price: float | None) -> str:
    if price is None:
        return "—"
    return f"{price:.{_decimals_for(symbol)}f}"


def _fmt_indicator(value: object) -> str:
    """Compact indicator value for inline lists. Floats get 4 decimals
    so prices round cleanly; small numbers show 2 decimals so RSI/ADX
    don't smear into noise."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, float):
        if abs(value) < 10:
            return f"{value:.2f}"
        if abs(value) < 100:
            return f"{value:.1f}"
        return f"{value:.4f}"
    return str(value)


def _fmt_money(amount: float, currency: str = "USD") -> str:
    sign = "+" if amount >= 0 else "−"
    return f"{sign}{abs(amount):.2f} {currency}".rstrip()


def _gate_icon(gate: str) -> str:
    g = (gate or "").lower()
    for key, icon in GATE_ICONS.items():
        if key in g:
            return icon
    return EMOJI["heat"]


@dataclass
class _ThrottleEntry:
    first_at: datetime
    last_at: datetime
    count: int = 0
    suppressed: int = 0  # number of repeats since the last send


class Notifier(Protocol):
    def send(self, text: str) -> bool: ...


class NoOpNotifier:
    def send(self, text: str) -> bool:
        log.debug("notifier disabled, would send: %s", text)
        return True


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        timeout_s: float = 10.0,
        setup_alert_window_min: int = 30,
    ) -> None:
        if not bot_token or not chat_id:
            raise ValueError("bot_token and chat_id required")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout_s = timeout_s
        # How long a setup_alert key stays throttled before we'll send
        # another one. Repeats during this window are counted and
        # surfaced as "(+N similar)" on the next message.
        self.setup_alert_window = timedelta(minutes=setup_alert_window_min)
        self._setup_throttle: dict[tuple[str, str, str], _ThrottleEntry] = {}

    def send(self, text: str) -> bool:
        url = API_URL.format(token=self.bot_token)
        data = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
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

    # --------------------------------------------------------------- startup
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
            f"<b>{EMOJI['online']} AntiGreed online</b>\n"
            f"Broker: <code>{broker} #{login}</code>\n"
            f"Server: <code>{server}</code>\n"
            f"Balance: <code>{balance:,.2f} {currency}</code>\n"
            f"Pairs: <code>{sym_str}</code>\n"
            f"Strategies: {strat_str}\n"
            f"Risk: <b>{risk_pct:.1%}</b>/trade · Daily cap <b>{max_daily_loss_pct:.0%}</b>"
        )

    def shutdown(self, *, reason: str = "manual stop") -> bool:
        return self.send(f"<b>{EMOJI['offline']} AntiGreed offline</b>\n<i>{reason}</i>")

    # --------------------------------------------------------------- trades
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
        side_u = side.upper()
        emoji = EMOJI["buy"] if side_u == "BUY" else EMOJI["sell"]
        lines = [f"{emoji} <b>OPEN {side_u} {symbol}</b> · <code>{lot_size:.2f}</code> lot"]
        lines.append(f"Entry <code>{_fmt_price(symbol, price)}</code>")
        if stop_loss is not None and take_profit is not None:
            sl_extra = f" ({sl_pips:.0f}p)" if sl_pips is not None else ""
            tp_extra = f" ({tp_pips:.0f}p)" if tp_pips is not None else ""
            lines.append(
                f"SL <code>{_fmt_price(symbol, stop_loss)}</code>{sl_extra}  ·  "
                f"TP <code>{_fmt_price(symbol, take_profit)}</code>{tp_extra}"
            )
        meta_bits: list[str] = []
        if risk_reward is not None:
            meta_bits.append(f"R:R <b>{risk_reward:.2f}</b>")
        if strategy:
            meta_bits.append(f"<i>{strategy.replace('_', ' ').title()}</i>")
        if regime:
            meta_bits.append(f"regime <code>{regime}</code>")
        if meta_bits:
            lines.append(" · ".join(meta_bits))
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
        side_u = side.upper()
        if abs(pnl) < 0.01:
            emoji = EMOJI["scratch"]
        elif pnl > 0:
            emoji = EMOJI["win"]
        else:
            emoji = EMOJI["loss"]
        sign = "+" if pnl >= 0 else "−"
        amount = f"<code>{sign}{abs(pnl):.2f}</code>"

        lines = [f"{emoji} <b>CLOSE {side_u} {symbol}</b> · {amount}"]
        if exit_price is not None:
            lines.append(
                f"Exit <code>{_fmt_price(symbol, exit_price)}</code> · {reason}"
            )
        else:
            lines.append(f"Reason: <i>{reason}</i>")

        meta: list[str] = []
        if hold_minutes is not None:
            meta.append(f"held {self._fmt_duration(hold_minutes)}")
        if strategy:
            meta.append(f"<i>{strategy.replace('_', ' ').title()}</i>")
        if meta:
            lines.append(" · ".join(meta))

        if today_pnl is not None and today_trades is not None:
            wr = (today_wins / today_trades) if today_trades and today_wins is not None else 0
            wr_text = f" · {wr:.0%} WR" if today_wins is not None else ""
            today_sign = "+" if today_pnl >= 0 else "−"
            lines.append(
                f"<i>Today: <code>{today_sign}{abs(today_pnl):.2f}</code> "
                f"on {today_trades} trade(s){wr_text}</i>"
            )
        return self.send("\n".join(lines))

    # --------------------------------------------------------------- daily / weekly
    def daily_summary(
        self,
        *,
        trades: int,
        wins: int,
        pnl: float,
        equity: float,
        best_pair: str | None = None,
        worst_pair: str | None = None,
    ) -> bool:
        win_rate = wins / trades if trades else 0
        emoji = EMOJI["win_day"] if pnl >= 0 else EMOJI["loss_day"]
        sign = "+" if pnl >= 0 else "−"
        lines = [
            f"{emoji} <b>Daily summary</b>",
            f"Trades <b>{trades}</b> · Wins <b>{wins}</b> ({win_rate:.0%})",
            f"P&amp;L <code>{sign}{abs(pnl):.2f}</code>",
            f"Equity <code>{equity:,.2f}</code>",
        ]
        if best_pair:
            lines.append(f"Best <code>{best_pair}</code>")
        if worst_pair and worst_pair != best_pair:
            lines.append(f"Worst <code>{worst_pair}</code>")
        return self.send("\n".join(lines))

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
        emoji = EMOJI["trophy"] if pnl >= 0 else EMOJI["broom"]
        sign = "+" if pnl >= 0 else "−"
        lines = [
            f"{emoji} <b>Weekly digest</b>",
            f"Trades <b>{trades}</b> · Wins <b>{wins}</b> ({win_rate:.0%})",
            f"P&amp;L <code>{sign}{abs(pnl):.2f}</code>",
            f"Equity <code>{equity:,.2f}</code>",
        ]
        if best_strategy:
            lines.append(f"Top strategy: <i>{best_strategy.replace('_', ' ').title()}</i>")
        if best_symbol:
            lines.append(f"Best <code>{best_symbol}</code>")
        if worst_symbol and worst_symbol != best_symbol:
            lines.append(f"Worst <code>{worst_symbol}</code>")
        return self.send("\n".join(lines))

    # --------------------------------------------------------------- alerts
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
            f"{EMOJI['calendar']} <b>{title}</b> · <code>{currency}</code>\n"
            f"In <b>{self._fmt_duration(minutes_until)}</b>"
            f" · pause <b>{before_min}m</b> before → <b>{after_min}m</b> after\n"
            f"Affects: <code>{pairs}</code>"
        )

    def signal_alert(
        self,
        *,
        symbol: str,
        side: str,
        strategy: str,
        price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        sl_pips: float | None = None,
        tp_pips: float | None = None,
        risk_reward: float | None = None,
        regime: str | None = None,
        reason: str | None = None,
        indicators: dict | None = None,
    ) -> bool:
        """Trade idea to act on manually — strategy is in 'signal' mode,
        so the bot is not placing this. Visually distinct from auto-open
        alerts so the user can tell them apart at a glance.
        """
        side_u = side.upper()
        side_emoji = EMOJI["buy"] if side_u == "BUY" else EMOJI["sell"]
        lines = [f"📡 <b>SIGNAL · {side_emoji} {side_u} {symbol}</b>"]
        lines.append(f"Entry <code>{_fmt_price(symbol, price)}</code>")
        if stop_loss is not None and take_profit is not None:
            sl_extra = f" ({sl_pips:.0f}p)" if sl_pips is not None else ""
            tp_extra = f" ({tp_pips:.0f}p)" if tp_pips is not None else ""
            lines.append(
                f"SL <code>{_fmt_price(symbol, stop_loss)}</code>{sl_extra}  ·  "
                f"TP <code>{_fmt_price(symbol, take_profit)}</code>{tp_extra}"
            )
        meta: list[str] = []
        if risk_reward is not None:
            meta.append(f"R:R <b>{risk_reward:.2f}</b>")
        meta.append(f"<i>{strategy.replace('_', ' ').title()}</i>")
        if regime:
            meta.append(f"regime <code>{regime}</code>")
        if meta:
            lines.append(" · ".join(meta))
        if reason:
            lines.append(f"<i>{reason}</i>")
        if indicators:
            ind_str = ", ".join(
                f"{k}={_fmt_indicator(v)}" for k, v in list(indicators.items())[:5]
            )
            lines.append(f"<i>Saw: {ind_str}</i>")
        lines.append("<i>Manual — bot is not placing this.</i>")
        return self.send("\n".join(lines))

    def setup_alert(
        self,
        *,
        symbol: str,
        side: str,
        strategy: str,
        gate: str,
        detail: str,
        price: float | None = None,
    ) -> bool:
        """Send a "setup gated" alert with throttling.

        Repeated alerts for the same (symbol, side, gate) are debounced
        to one every `setup_alert_window` (default 30 min). Repeats
        within the window are counted and surfaced on the next send as
        "<i>+N similar suppressed</i>" so the user sees activity without
        being spammed.
        """
        now = datetime.now(timezone.utc)
        gate_key = self._gate_key(gate)
        key = (symbol.upper(), side.upper(), gate_key)
        entry = self._setup_throttle.get(key)
        if entry is not None and (now - entry.last_at) < self.setup_alert_window:
            entry.last_at = now
            entry.count += 1
            entry.suppressed += 1
            return True  # silent: hit telegram quota / spam ceiling

        suppressed_before = entry.suppressed if entry else 0
        self._setup_throttle[key] = _ThrottleEntry(
            first_at=entry.first_at if entry else now,
            last_at=now,
            count=(entry.count + 1) if entry else 1,
            suppressed=0,
        )

        side_u = side.upper()
        side_emoji = EMOJI["buy"] if side_u == "BUY" else EMOJI["sell"]
        gate_emoji = _gate_icon(gate)
        lines = [
            f"{gate_emoji} <b>Skipped {side_emoji} {side_u} {symbol}</b>"
            f" — <code>{_fmt_price(symbol, price)}</code>"
            if price is not None
            else f"{gate_emoji} <b>Skipped {side_emoji} {side_u} {symbol}</b>",
            f"<i>{strategy.replace('_', ' ').title()}</i>"
            f" · gated by <b>{gate}</b>",
            f"<i>{detail}</i>",
        ]
        if suppressed_before > 0:
            window_min = int(self.setup_alert_window.total_seconds() // 60)
            lines.append(
                f"<i>+{suppressed_before} similar suppressed in last {window_min}m</i>"
            )
        return self.send("\n".join(lines))

    @staticmethod
    def _gate_key(gate: str) -> str:
        """Bucket gate strings into stable keys for throttling.

        Avoids treating "portfolio heat 12.3%" and "portfolio heat 14.7%"
        as different reasons — they're the same gate firing twice.
        """
        g = (gate or "").lower()
        for token in ("portfolio heat", "risk manager", "blackout",
                      "calendar", "regime", "cooldown", "kill switch"):
            if token in g:
                return token
        return g.split(":")[0].strip()

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _fmt_duration(minutes: float) -> str:
        if minutes < 1:
            return "<1 min"
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
