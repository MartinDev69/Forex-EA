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
    STATE_AWAITING_EMAIL,
    STATE_IDLE,
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

UNKNOWN_COMMAND = (
    "I didn't understand that. Type /start to begin a new subscription "
    "request, or wait for an admin response if you've already submitted one."
)

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
                 reply_markup: dict | None = None) -> bool:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _api_call(token, "sendMessage", payload, timeout=10) is not None


def send_approval_dm(
    token: str | None, chat_id: int, ad_id: str, duration: str,
) -> bool:
    if not token:
        return False
    return send_message(
        token, chat_id,
        APPROVAL_DM.format(ad_id=ad_id, duration_label=DURATION_LABEL.get(duration, duration)),
    )


def send_approval_dm_with_link(
    token: str | None, chat_id: int, ad_id: str, duration: str,
    setup_url: str, expires_hours: int,
) -> bool:
    """Like send_approval_dm but embeds the setup link directly in the
    Telegram message — used when the admin opts to skip email delivery
    and hand the link straight to the user via the bot.
    """
    if not token:
        return False
    return send_message(
        token, chat_id,
        APPROVAL_DM_WITH_LINK.format(
            ad_id=ad_id,
            duration_label=DURATION_LABEL.get(duration, duration),
            setup_url=setup_url,
            expires_hours=expires_hours,
        ),
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
                 admin_chat_id: int | None = None) -> None:
        self.token = token
        self.store = store
        # admin_chat_id is kept for reference / future use, but no
        # longer filters incoming DMs — we used to drop messages whose
        # chat_id matched the admin's, on the assumption the admin
        # didn't need to sign up via their own bot. But Telegram
        # private-chat IDs are the user's own user_id, so the admin
        # was being silently filtered out when testing /start.
        self.admin_chat_id = admin_chat_id
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

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
                chat_id, state=STATE_AWAITING_EMAIL, duration=code,
                username=username, first_name=first_name,
            )
            send_message(
                self.token, chat_id,
                EMAIL_PROMPT.format(duration_label=DURATION_LABEL.get(code, code)),
            )

    # -------------------------------------------------- conversation steps

    def _send_welcome(self, chat_id: int, *, username: str | None,
                      first_name: str | None) -> None:
        self.store.upsert_state(
            chat_id, state=STATE_IDLE, duration=None,
            username=username, first_name=first_name,
        )
        send_message(self.token, chat_id, WELCOME_MESSAGE,
                     reply_markup=DURATION_KEYBOARD)

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
