"""Telegram bot for collecting subscription requests.

Distinct from the alerts notifier — that one only sends, this one
receives DMs from public users and walks them through a small
conversation:

  /start   → warm welcome + inline keyboard with the six durations
  pick dur → bot asks for email; sets chat state to awaiting_email
  send email → bot validates + creates a pending row + confirms

When the admin later approves a request via the API, server.py calls
``send_approval_dm`` to tell the user to check their inbox.

Polling design — long-poll ``getUpdates?timeout=30`` from a single
asyncio task in the API process. update_id offsets are persisted, so
a restart picks up exactly where the previous run left off without
re-processing or losing any updates.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Any

from .subscription_requests import (
    PICKS_REQUIRED,
    STATE_AWAITING_EMAIL,
    STATE_AWAITING_PHONE,
    STATE_IDLE,
    STATE_PICKING_EXECUTE,
    STATE_PICKING_SIGNALS,
    SubscriptionRequestStore,
    VALID_DURATIONS,
)

# Route through uvicorn.error so messages reach api.stderr.log without
# having to call logging.basicConfig (which would fight uvicorn's setup).
log = logging.getLogger("uvicorn.error")

API_URL = "https://api.telegram.org/bot{token}/{method}"

DURATION_LABEL = {
    "5h":  "5 hours",
    "1w":  "1 week",
    "2w":  "2 weeks",
    "1m":  "1 month",
    "2m":  "2 months",
    "3m":  "3 months",
}

# Inline keyboard layout for the duration prompt — two columns, three
# rows so it fits comfortably in the Telegram chat.
DURATION_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "⏱  5 hours",  "callback_data": "dur:5h"},
            {"text": "📅  1 week",   "callback_data": "dur:1w"},
        ],
        [
            {"text": "📅  2 weeks",  "callback_data": "dur:2w"},
            {"text": "📅  1 month",  "callback_data": "dur:1m"},
        ],
        [
            {"text": "📅  2 months", "callback_data": "dur:2m"},
            {"text": "📅  3 months", "callback_data": "dur:3m"},
        ],
    ]
}

WELCOME_MESSAGE = (
    "👋 <b>Welcome to AntiGreed!</b>\n\n"
    "We're an autonomous FX/commodity trading bot — quiet, regime-aware, "
    "risk-capped. Sign up below to get an access ID for the dashboard.\n\n"
    "<b>How long would you like access for?</b>"
)

PHONE_PROMPT = (
    "Great choice — <b>{duration_label}</b>.\n\n"
    "Tap the button below to share your phone number with us. "
    "We'll use it to keep your account on file."
)

# Custom keyboard with a single "Share my phone number" button. Telegram
# replies with a contact object (phone_number, first_name, last_name)
# in the next message — no typing required.
SHARE_PHONE_KEYBOARD = {
    "keyboard": [[{"text": "📱 Share my phone number", "request_contact": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

# Used to remove the custom keyboard once we've captured the contact.
REMOVE_KEYBOARD = {"remove_keyboard": True}

EMAIL_PROMPT = (
    "Great choice — <b>{duration_label}</b>.\n\n"
    "Please reply with the email address you'd like the setup link sent to. "
    "It only takes one message — just type the email and hit send."
)

REQUEST_CONFIRMED = (
    "✅ <b>Request received.</b>\n\n"
    "I've sent your request to the admin. Once they approve it you'll "
    "get an email with a link to set your password — and I'll DM you "
    "here to let you know it's ready.\n\n"
    "<i>Hold tight, this usually takes less than a few hours during business time.</i>"
)

INVALID_EMAIL = (
    "⚠️ That doesn't look like a valid email address. "
    "Please try again — just type the email and send."
)

NEED_CONTACT = (
    "⚠️ I need you to tap the <b>📱 Share my phone number</b> button below "
    "to continue. (Typing the number doesn't work — Telegram needs you "
    "to tap the button so it knows you really own the number.)"
)

UNKNOWN_COMMAND = (
    "I didn't understand that. Type /start to begin a new subscription "
    "request, or wait for an admin response if you've already submitted one."
)

# Display labels for the strategy picker keyboards. Keys match the
# names registered by src.strategies.STRATEGY_REGISTRY.
STRATEGY_LABELS = {
    "ma_crossover":        "MA Crossover",
    "rsi_mean_reversion":  "RSI Mean-Reversion",
    "donchian_breakout":   "Donchian Breakout",
    "macd_cross":          "MACD Cross",
    "bollinger_bounce":    "Bollinger Bounce",
    "bollinger_squeeze":   "Bollinger Squeeze",
    "stochastic_reversal": "Stochastic Reversal",
    "triple_ma_alignment": "Triple MA",
    "inside_bar_breakout": "Inside-Bar Breakout",
    "engulfing_pattern":   "Engulfing Pattern",
    "ema_pullback":        "EMA Pullback",
    "adx_breakout":        "ADX Breakout",
}

PICK_INTRO_SIGNAL = (
    "🎯 <b>Pick 3 strategies for SIGNAL ALERTS</b>\n\n"
    "These are the strategies you'll get Telegram alerts for. The bot "
    "won't auto-place them on your account — you decide what to do "
    "with each signal.\n\n"
    "Tap to toggle. <i>Selected: {n} / 3</i>"
)
PICK_INTRO_EXECUTE = (
    "⚙️ <b>Pick 2 strategies for AUTO-EXECUTE</b>\n\n"
    "These are the strategies your copy-trading EA will replicate on "
    "your MT5 account automatically. Choose the two you trust most.\n\n"
    "Tap to toggle. <i>Selected: {n} / 2</i>"
)
PICK_LOCKED_NOTE = (
    "<i>Your picks are locked for the subscription term — choose carefully.</i>"
)
PICK_COMPLETE_PROMPT = "✅ Tap <b>Continue</b> when you're happy with your selection."

APPROVAL_DM = (
    "🎉 <b>Your AntiGreed access is ready!</b>\n\n"
    "Your AD-ID <code>{ad_id}</code> has been assigned with a "
    "<b>{duration_label}</b> subscription.\n\n"
    "📧 Check your email — we just sent a setup link. Click it to choose "
    "your password, then log in to the dashboard.\n\n"
    "<i>The setup link is valid for 24 hours.</i>"
)

# Variant used when the admin chose to deliver the setup link via
# Telegram instead of email. Drops the email reference and embeds the
# link directly so the user can tap it.
APPROVAL_DM_WITH_LINK = (
    "🎉 <b>Your AntiGreed access is ready!</b>\n\n"
    "Your AD-ID <code>{ad_id}</code> has been assigned with a "
    "<b>{duration_label}</b> subscription.\n\n"
    "🔐 Tap the link below to set your password, then log in to the "
    "dashboard:\n\n"
    "<a href=\"{setup_url}\">Set my password</a>\n\n"
    "<i>The link is valid for {expires_hours} hours.</i>"
)

# Appended to either approval DM when the notifier bot's @username is
# configured. The signup bot can't DM the operator with trade alerts
# directly — that's a separate bot under TELEGRAM_BOT_TOKEN — and that
# bot can't message the operator until *they* press Start once. We hand
# them a deep link so it's a single tap.
NOTIFIER_LINK_LINE = (
    "\n\n🔔 <b>One more step — enable trade alerts:</b>\n"
    "Tap <a href=\"https://t.me/{notifier_username}?start={ad_id}\">"
    "open @{notifier_username}</a> and press <b>Start</b>. "
    "After that you'll receive a DM for each trade on the strategies "
    "you picked."
)

REJECTION_DM = (
    "❌ <b>Your subscription request was declined.</b>\n\n"
    "{reason}\n\n"
    "If you think this is a mistake, you can submit a new request with /start."
)

EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")


def _api_call(token: str, method: str, payload: dict[str, Any] | None = None,
              timeout: int = 35) -> dict[str, Any] | None:
    """Minimal Telegram Bot API client — POSTs JSON, returns the
    decoded ``result`` field. Returns None on transport error so the
    polling loop can keep going.

    Logs every non-OK Telegram response so the operator can debug
    when the bot is silent (typical: 401 Unauthorized = wrong token,
    409 Conflict = webhook still set).
    """
    url = API_URL.format(token=token, method=method)
    data: bytes | None = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Surface the response body — Telegram returns the actual
        # reason ("Unauthorized", "Conflict: webhook is currently set",
        # etc.) here, which is what the operator needs to fix the issue.
        try:
            body = json.loads(e.read().decode("utf-8"))
            log.warning(
                "telegram %s -> HTTP %s: %s",
                method, e.code, body.get("description"),
            )
        except Exception:
            log.warning("telegram %s -> HTTP %s", method, e.code)
        return None
    except Exception as exc:
        log.warning("telegram %s transport error: %s", method, exc)
        return None
    if not body.get("ok"):
        log.warning("telegram %s rejected: %s", method, body.get("description"))
        return None
    return body.get("result")


def send_message(token: str, chat_id: int, text: str,
                 reply_markup: dict | None = None) -> int | None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    result = _api_call(token, "sendMessage", payload, timeout=10)
    if result is None:
        return None
    return result.get("message_id")


def edit_message_text(token: str, chat_id: int, message_id: int,
                      text: str, reply_markup: dict | None = None) -> bool:
    """Edit an existing bot message — used to refresh the strategy
    picker's checkmark grid in place instead of spamming new messages."""
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _api_call(token, "editMessageText", payload, timeout=10) is not None


def build_picker_keyboard(
    strategies: list[str],
    selected: list[str],
    kind: str,
    *,
    show_continue: bool,
) -> dict:
    """Two-per-row inline checkbox grid + an optional Continue button.
    `kind` is 'signal' or 'execute' — routes the callback prefix."""
    rows: list[list[dict]] = []
    row: list[dict] = []
    sel = set(selected)
    for s in strategies:
        marker = "☑" if s in sel else "☐"
        label = STRATEGY_LABELS.get(s, s.replace("_", " ").title())
        row.append({"text": f"{marker}  {label}", "callback_data": f"pick:{kind}:{s}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if show_continue:
        rows.append([{"text": "✅  Continue →", "callback_data": f"pickdone:{kind}"}])
    return {"inline_keyboard": rows}


def _notifier_link_suffix(notifier_username: str | None, ad_id: str) -> str:
    if not notifier_username:
        return ""
    return NOTIFIER_LINK_LINE.format(
        notifier_username=notifier_username.lstrip("@"), ad_id=ad_id,
    )


def send_approval_dm(
    token: str | None, chat_id: int, ad_id: str, duration: str,
    notifier_username: str | None = None,
) -> bool:
    if not token:
        return False
    body = APPROVAL_DM.format(
        ad_id=ad_id, duration_label=DURATION_LABEL.get(duration, duration),
    )
    return send_message(
        token, chat_id, body + _notifier_link_suffix(notifier_username, ad_id),
    )


def send_approval_dm_with_link(
    token: str | None, chat_id: int, ad_id: str, duration: str,
    setup_url: str, expires_hours: int,
    notifier_username: str | None = None,
) -> bool:
    """Like send_approval_dm but embeds the setup link directly in the
    Telegram message — used when the admin opts to skip email delivery
    and hand the link straight to the user via the bot.
    """
    if not token:
        return False
    body = APPROVAL_DM_WITH_LINK.format(
        ad_id=ad_id,
        duration_label=DURATION_LABEL.get(duration, duration),
        setup_url=setup_url,
        expires_hours=expires_hours,
    )
    return send_message(
        token, chat_id, body + _notifier_link_suffix(notifier_username, ad_id),
    )


def send_rejection_dm(token: str | None, chat_id: int, reason: str) -> bool:
    if not token:
        return False
    return send_message(
        token, chat_id,
        REJECTION_DM.format(reason=reason or "No reason given."),
    )


class TelegramSignupBot:
    """Long-polling Telegram bot driving the subscription-signup flow.

    Owns: the polling task, conversation state in SQLite, the request
    store. Doesn't know anything about the rest of the API — server.py
    calls ``send_approval_dm`` directly from the approve endpoint.
    """

    def __init__(self, token: str, store: SubscriptionRequestStore,
                 admin_chat_id: int | None = None,
                 toggle_store=None) -> None:
        self.token = token
        self.store = store
        # admin_chat_id is kept for reference / future use, but no
        # longer filters incoming DMs — we used to drop messages whose
        # chat_id matched the admin's, on the assumption the admin
        # didn't need to sign up via their own bot. But Telegram
        # private-chat IDs are the user's own user_id, so the admin
        # was being silently filtered out when testing /start.
        self.admin_chat_id = admin_chat_id
        # StrategyToggleStore — used to enumerate user-copyable
        # strategies for the per-user picker keyboards.
        self.toggle_store = toggle_store
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Phone number captured at the start of the picker stage. Held
        # in memory rather than the chat_state DB because the picker is
        # short-lived; a bot restart between the contact share and the
        # final pick is expected to be rare and the user can /start over.
        self._phone_cache: dict[int, str] = {}
        # The message_id of the active picker so we can edit it in
        # place as the user toggles checkmarks. Keyed by chat_id.
        self._picker_msg: dict[int, int] = {}

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    # -------------------------------------------------- polling loop

    async def _run(self) -> None:
        log.info("signup bot _run task entered")
        loop = asyncio.get_running_loop()

        # Identity check — call getMe so the operator knows the token is
        # valid and which bot username it belongs to. Then delete any
        # existing webhook so getUpdates doesn't 409. Both calls are
        # one-shot, fast, and best-effort: if Telegram is unreachable
        # the polling loop will keep retrying anyway.
        try:
            me = await loop.run_in_executor(None, _api_call, self.token, "getMe", None, 10)
            if me:
                log.info(
                    "signup bot identity: @%s (id=%s, name=%s)",
                    me.get("username"), me.get("id"), me.get("first_name"),
                )
            else:
                log.warning(
                    "signup bot getMe returned nothing — token may be invalid; "
                    "verify with: curl https://api.telegram.org/bot<TOKEN>/getMe"
                )
        except Exception:
            log.exception("signup bot getMe failed")

        try:
            await loop.run_in_executor(
                None, _api_call, self.token, "deleteWebhook",
                {"drop_pending_updates": False}, 10,
            )
        except Exception:
            log.exception("signup bot deleteWebhook failed")

        log.info("signup bot polling loop running")
        while not self._stop.is_set():
            offset = self.store.get_update_offset()
            try:
                # urllib is sync; run it in the default executor so the
                # 30s long-poll doesn't block other API tasks.
                updates = await loop.run_in_executor(
                    None, self._fetch_updates, offset + 1,
                )
            except Exception:
                log.exception("getUpdates failed; backing off 5s")
                await asyncio.sleep(5)
                continue
            if not updates:
                continue
            for update in updates:
                try:
                    self._handle_update(update)
                except Exception:
                    log.exception("handle_update failed for %s", update)
                update_id = int(update.get("update_id", 0))
                if update_id:
                    self.store.set_update_offset(update_id)

    def _fetch_updates(self, offset: int) -> list[dict]:
        result = _api_call(
            self.token, "getUpdates",
            {
                "offset": offset,
                "timeout": 30,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=35,
        )
        return result or []

    # -------------------------------------------------- update routing

    def _handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
        elif "message" in update:
            self._handle_message(update["message"])

    # -------------------------------------------------- message handling

    def _handle_message(self, message: dict) -> None:
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        if chat_id == 0:
            return

        from_user = message.get("from") or {}
        username = from_user.get("username")
        first_name = from_user.get("first_name")
        text = (message.get("text") or "").strip()

        # Always refresh the user's profile bits — easier to identify
        # them in the admin-side pending list.
        self.store.upsert_state(
            chat_id, state=self._current_state(chat_id),
            duration=self._current_duration(chat_id),
            username=username, first_name=first_name,
        )

        if text == "/start":
            self._send_welcome(chat_id, username=username, first_name=first_name)
            return

        state = self._current_state(chat_id)

        # Contact share — fires when the user tapped the "Share my phone
        # number" button. The phone number arrives as a structured field,
        # not text.
        if "contact" in message:
            contact = message["contact"] or {}
            phone = (contact.get("phone_number") or "").strip()
            if state == STATE_AWAITING_PHONE and phone:
                self._handle_phone(
                    chat_id, phone,
                    username=username, first_name=first_name,
                )
                return

        if state == STATE_AWAITING_PHONE:
            # User typed something instead of tapping the button. Telegram
            # only authenticates contacts via the share flow, so we have
            # to ask again.
            send_message(self.token, chat_id, NEED_CONTACT,
                         reply_markup=SHARE_PHONE_KEYBOARD)
            return

        if state == STATE_AWAITING_EMAIL:
            self._handle_email(chat_id, text, username=username, first_name=first_name)
            return

        send_message(self.token, chat_id, UNKNOWN_COMMAND)

    def _handle_callback(self, cb: dict) -> None:
        from_user = cb.get("from") or {}
        chat_id = int((cb.get("message") or {}).get("chat", {}).get("id", 0))
        if chat_id == 0:
            return
        data = cb.get("data") or ""
        username = from_user.get("username")
        first_name = from_user.get("first_name")

        # Acknowledge the callback so Telegram dismisses the loading
        # spinner on the user's tap.
        _api_call(self.token, "answerCallbackQuery", {"callback_query_id": cb.get("id")},
                  timeout=10)

        if data.startswith("dur:"):
            code = data.split(":", 1)[1]
            if code not in VALID_DURATIONS:
                send_message(self.token, chat_id,
                             "That option isn't available — please /start over.")
                return
            self.store.upsert_state(
                chat_id, state=STATE_AWAITING_PHONE, duration=code,
                username=username, first_name=first_name,
            )
            send_message(
                self.token, chat_id,
                PHONE_PROMPT.format(duration_label=DURATION_LABEL.get(code, code)),
                reply_markup=SHARE_PHONE_KEYBOARD,
            )
            return

        # Strategy picker — toggle a single checkbox.
        if data.startswith("pick:"):
            parts = data.split(":", 2)
            if len(parts) != 3:
                return
            _, kind, strategy = parts
            if kind not in ("signal", "execute"):
                return
            self._handle_pick_toggle(
                chat_id, kind, strategy,
                username=username, first_name=first_name,
            )
            return

        # Strategy picker — Continue button: advance round or finalise.
        if data.startswith("pickdone:"):
            kind = data.split(":", 1)[1]
            if kind not in ("signal", "execute"):
                return
            self._handle_pick_done(
                chat_id, kind, username=username, first_name=first_name,
            )
            return

    # -------------------------------------------------- conversation steps

    def _send_welcome(self, chat_id: int, *, username: str | None,
                      first_name: str | None) -> None:
        self.store.upsert_state(
            chat_id, state=STATE_IDLE, duration=None,
            username=username, first_name=first_name,
        )
        send_message(self.token, chat_id, WELCOME_MESSAGE,
                     reply_markup=DURATION_KEYBOARD)

    def _handle_phone(self, chat_id: int, phone: str, *,
                      username: str | None, first_name: str | None) -> None:
        """Captured phone number from the contact-share button. Stashes
        the phone, advances to the strategy-picker stage. The
        subscription request itself isn't created until both picker
        rounds are complete."""
        # Telegram contacts often arrive without a leading + on some
        # carriers; normalize so the dashboard always shows it that way.
        clean = phone.strip()
        if clean and not clean.startswith("+"):
            clean = "+" + clean.lstrip("0")
        duration = self._current_duration(chat_id)
        if duration is None:
            self._send_welcome(chat_id, username=username, first_name=first_name)
            return
        # Cache the phone in the user's chat_state duration column —
        # we already have a free-form text slot there — by stuffing it
        # into a private state key. Reuse first_name to keep it simple:
        # leave as-is, store phone separately via the picks_signal slot
        # repurposed as "phone temp"? Cleaner: keep duration; phone
        # number lives in the chat_state row via a tiny migration too.
        # For now we hold the phone in the picker callback chain using
        # the state row's "duration" tag — but `duration` is needed for
        # the create_request call later. We need a separate slot.
        # Cheapest path: dismiss the share-keyboard, stash the phone on
        # the request-state-pending list directly by reusing first_name.
        # That breaks display, so instead we add a dedicated cache.
        # Pragmatic: keep the phone in memory on this instance keyed by
        # chat_id. Survives the picker round-trip; on bot restart the
        # user re-/start's anyway.
        self._phone_cache[chat_id] = clean
        self.store.upsert_state(
            chat_id, state=STATE_PICKING_SIGNALS, duration=duration,
            username=username, first_name=first_name,
        )
        self.store.set_state_picks(chat_id, signal=[], execute=[])
        # Dismiss the share-phone reply keyboard with a tiny ack-style
        # message so the picker arrives as a single, self-contained
        # message — was sending the picker intro twice before.
        send_message(self.token, chat_id,
                     "📞 Got it — phone saved.",
                     reply_markup=REMOVE_KEYBOARD)
        self._send_picker(chat_id, kind="signal", selected=[])
        log.info("phone captured for chat=%s, advancing to signal picker", chat_id)

    # ---------- strategy picker ----------

    def _available_strategies(self) -> list[str]:
        """Strategies the user may pick from. Filtered to those admin
        has marked user_copyable so the picker doesn't expose
        admin-only strategies. Order matches STRATEGY_LABELS so the
        layout is stable across signups.
        """
        if self.toggle_store is None:
            return list(STRATEGY_LABELS.keys())
        try:
            available = self.toggle_store.user_copyable_names()
        except Exception:
            log.exception("toggle_store.user_copyable_names failed")
            return list(STRATEGY_LABELS.keys())
        # Preserve declared display order so users always see the same
        # grid regardless of how SQLite returned the rows.
        return [s for s in STRATEGY_LABELS if s in available]

    def _send_picker(self, chat_id: int, *, kind: str, selected: list[str]) -> None:
        strategies = self._available_strategies()
        required = PICKS_REQUIRED[kind]
        intro = (PICK_INTRO_SIGNAL if kind == "signal" else PICK_INTRO_EXECUTE)
        body = intro.format(n=len(selected))
        if len(selected) >= required:
            body += "\n\n" + PICK_COMPLETE_PROMPT
        body += "\n\n" + PICK_LOCKED_NOTE
        keyboard = build_picker_keyboard(
            strategies, selected, kind,
            show_continue=len(selected) >= required,
        )
        msg_id = send_message(self.token, chat_id, body, reply_markup=keyboard)
        if msg_id is not None:
            self._picker_msg[chat_id] = msg_id

    def _refresh_picker(self, chat_id: int, *, kind: str, selected: list[str]) -> None:
        strategies = self._available_strategies()
        required = PICKS_REQUIRED[kind]
        intro = (PICK_INTRO_SIGNAL if kind == "signal" else PICK_INTRO_EXECUTE)
        body = intro.format(n=len(selected))
        if len(selected) >= required:
            body += "\n\n" + PICK_COMPLETE_PROMPT
        body += "\n\n" + PICK_LOCKED_NOTE
        keyboard = build_picker_keyboard(
            strategies, selected, kind,
            show_continue=len(selected) >= required,
        )
        msg_id = self._picker_msg.get(chat_id)
        if msg_id is None:
            self._send_picker(chat_id, kind=kind, selected=selected)
            return
        ok = edit_message_text(
            self.token, chat_id, msg_id, body, reply_markup=keyboard,
        )
        if not ok:
            # Editing failed (rare — usually because Telegram has already
            # GC'd the original message). Fall back to a fresh send.
            self._send_picker(chat_id, kind=kind, selected=selected)

    def _handle_pick_toggle(
        self, chat_id: int, kind: str, strategy: str,
        *, username: str | None, first_name: str | None,
    ) -> None:
        state = self.store.get_state(chat_id)
        if state is None:
            return
        # Validate that the user is in the right picker round for this
        # kind. If they're in execute round but tapped a signal button
        # (shouldn't happen via Telegram but defensive), bounce.
        expected = (STATE_PICKING_SIGNALS if kind == "signal" else STATE_PICKING_EXECUTE)
        if state.state != expected:
            return
        required = PICKS_REQUIRED[kind]
        current = list(state.picks_signal if kind == "signal" else state.picks_execute)
        if strategy in current:
            current.remove(strategy)
        elif len(current) < required:
            current.append(strategy)
        else:
            # Already at the limit — ignore the extra tap.
            return
        if kind == "signal":
            self.store.set_state_picks(chat_id, signal=current)
        else:
            self.store.set_state_picks(chat_id, execute=current)
        self._refresh_picker(chat_id, kind=kind, selected=current)

    def _handle_pick_done(
        self, chat_id: int, kind: str,
        *, username: str | None, first_name: str | None,
    ) -> None:
        state = self.store.get_state(chat_id)
        if state is None:
            return
        if kind == "signal":
            if len(state.picks_signal) != PICKS_REQUIRED["signal"]:
                return
            # Advance to the execute picker.
            self.store.upsert_state(
                chat_id, state=STATE_PICKING_EXECUTE, duration=state.duration,
                username=username, first_name=first_name,
            )
            self._picker_msg.pop(chat_id, None)
            self._send_picker(chat_id, kind="execute", selected=list(state.picks_execute))
            return
        # kind == "execute"
        if len(state.picks_execute) != PICKS_REQUIRED["execute"]:
            return
        self._finalize_signup(chat_id, username=username, first_name=first_name)

    def _finalize_signup(
        self, chat_id: int, *,
        username: str | None, first_name: str | None,
    ) -> None:
        state = self.store.get_state(chat_id)
        if state is None:
            return
        phone = self._phone_cache.pop(chat_id, None)
        duration = state.duration
        if duration is None:
            self._send_welcome(chat_id, username=username, first_name=first_name)
            return
        try:
            self.store.create_request(
                chat_id=chat_id, username=username, first_name=first_name,
                duration=duration, email="", phone_number=phone,
                picks_signal=list(state.picks_signal),
                picks_execute=list(state.picks_execute),
            )
        except ValueError:
            send_message(self.token, chat_id,
                         "Sorry, that request couldn't be saved. /start to try again.",
                         reply_markup=REMOVE_KEYBOARD)
            return
        self.store.upsert_state(
            chat_id, state=STATE_IDLE, duration=None,
            username=username, first_name=first_name,
        )
        self.store.set_state_picks(chat_id, signal=[], execute=[])
        self._picker_msg.pop(chat_id, None)
        send_message(self.token, chat_id, REQUEST_CONFIRMED,
                     reply_markup=REMOVE_KEYBOARD)
        log.info(
            "signup request from chat=%s phone=%s duration=%s signals=%s execute=%s",
            chat_id, phone, duration,
            ",".join(state.picks_signal), ",".join(state.picks_execute),
        )

    def _handle_email(self, chat_id: int, text: str, *,
                      username: str | None, first_name: str | None) -> None:
        email = text.strip()
        if not EMAIL_RE.match(email):
            send_message(self.token, chat_id, INVALID_EMAIL)
            return
        duration = self._current_duration(chat_id)
        if duration is None:
            # Edge case: state got out of sync. Restart them.
            self._send_welcome(chat_id, username=username, first_name=first_name)
            return
        try:
            self.store.create_request(
                chat_id=chat_id, username=username, first_name=first_name,
                duration=duration, email=email,
            )
        except ValueError:
            send_message(self.token, chat_id, "Sorry, that request couldn't be saved. /start to try again.")
            return
        # Reset to idle so a stray follow-up message doesn't get treated
        # as another email.
        self.store.upsert_state(
            chat_id, state=STATE_IDLE, duration=None,
            username=username, first_name=first_name,
        )
        send_message(self.token, chat_id, REQUEST_CONFIRMED)
        log.info("subscription request from chat=%s email=%s duration=%s",
                 chat_id, email, duration)

    # -------------------------------------------------- helpers

    def _current_state(self, chat_id: int) -> str:
        s = self.store.get_state(chat_id)
        return s.state if s else STATE_IDLE

    def _current_duration(self, chat_id: int) -> str | None:
        s = self.store.get_state(chat_id)
        return s.duration if s else None
