"""FastAPI backend — what the dashboard + AntiGreed mobile app talk to.

The bot writes trade events into data/trades.db; this server reads from
that same SQLite file so the UI reflects reality without needing IPC with
the bot process.

Endpoints:
  POST /auth/login      issue JWT
  GET  /auth/me         who am I (requires token)
  GET  /health          (unauthenticated — liveness probe)
  GET  /status          bot running? last heartbeat?
  GET  /account         balance/equity/open positions/daily P&L
  GET  /trades          recent trade history (from SQLite journal)
  GET  /strategies      list enabled flags
  POST /strategies/{name}/toggle
  POST /bot/start
  POST /bot/stop
  GET  /                dashboard SPA (static HTML)

Run locally:
  AUTH_SECRET=$(python -c 'import secrets;print(secrets.token_urlsafe(48))') \
    uvicorn src.api.server:app --reload --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

# Pull .env into os.environ before any module reads SMTP_*, PUBLIC_BASE_URL,
# AUTH_SECRET, etc. The bot process gets this for free via load_settings();
# the API was missing it, so SMTP and setup-link config were silently empty
# under NSSM where only PYTHONUTF8 is injected explicitly.
load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from src.api import brokers as broker_presets
from src.api.auth import (
    LoginRateLimiter,
    _secret,
    authenticate,
    client_ip,
    create_token,
    current_user as _current_user_jwt,
    hash_password,
    rate_limiter,
    require_admin as _require_admin_jwt,
)
from src.api.ad_id import ADMIN_AD_ID, is_user_ad_id
from src.api.broker_config import BrokerConfig, BrokerConfigStore
from src.api.broker_status import BrokerStatusStore
from src.api.pending_orders import PendingOrderStore
from src.api.mailer import send_setup_email
from src.api.setup_tokens import SETUP_TTL_S, create_setup_token, decode_setup_token
from src.api.totp import generate_secret, provisioning_uri, verify_code
from src.api.totp_store import TOTPStore
from src.api.ea_signals import SignalFeed
from src.api.ea_account_reports import EAAccountReportStore
from src.api.bot_control import BotControlStore
from src.api.subscription_requests import (
    SubscriptionRequest,
    SubscriptionRequestStore,
)
from src.api.telegram_signup import (
    TelegramSignupBot,
    send_approval_dm,
    send_approval_dm_with_link,
    send_rejection_dm,
)
from src.api.users import LastAdminError, UserStore, parse_duration
from src.allocator import AllocationStore
from src.explanations import TradeExplanationStore
from src.correlation import CorrelationStore
from src.drift import BaselineStore, DriftConfig, DriftMonitor
from src.econ_calendar import BlackoutChecker, BlackoutPolicy, EventStore, ForexFactoryProvider
from src.econ_calendar.refresher import CalendarRefresher
from src.execution.fills import FillStore
from src.execution.journal import TradeJournal
from src.execution.strategy_toggles import DEFAULT_STRATEGY_FLAGS, StrategyToggleStore
from src.narrator import NarrativeStore
from src.propfirm import PropFirmGuard, PropFirmStore, policy_from_env
from src.replay import PathStore, ReplayEngine, ReplayRequest
from src.regime import RegimeStore, empty_snapshot_dict
from src.strategies import STRATEGY_REGISTRY
from src.voice import (
    KillSwitchFlag,
    VoiceKillConfig,
    VoiceLogStore,
    match_phrase,
)
from src.watchdog import HeartbeatStore

import jwt as _jwt  # noqa: E402  — for exception types in /auth/setup

# Uvicorn pre-configures "uvicorn.error" at INFO with a handler that
# writes to stderr — so app-level INFO messages actually appear in
# api.stderr.log without us having to call logging.basicConfig (which
# would fight uvicorn's own log config).
log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _calendar_refresher, _expiry_notifier, _signup_bot
    interval = int(os.environ.get("CALENDAR_REFRESH_INTERVAL_S", "1800"))
    _calendar_refresher = CalendarRefresher(
        ForexFactoryProvider(), calendar_store, interval_s=interval,
    )
    _calendar_refresher.start()
    # Subscription-expiry scan — every 15 min, look for users whose
    # subscription just lapsed but who haven't been emailed yet, send
    # the "your subscription has expired" notice, mark them notified.
    _expiry_notifier = asyncio.create_task(_subscription_expiry_loop())
    # Telegram signup bot — receives DMs from prospective users and
    # walks them through the duration → email flow. Only starts if
    # SIGNUP_TELEGRAM_BOT_TOKEN is set; otherwise the dashboard's
    # admin Operators panel is the only signup path.
    if SIGNUP_BOT_TOKEN:
        _signup_bot = TelegramSignupBot(
            SIGNUP_BOT_TOKEN, subscription_request_store,
            admin_chat_id=ADMIN_TG_CHAT_ID,
        )
        _signup_bot.start()
        # Mask all but the last 6 chars so the operator can confirm
        # the right token was loaded without leaking the full secret
        # to the log.
        masked = "***" + SIGNUP_BOT_TOKEN[-6:] if len(SIGNUP_BOT_TOKEN) > 6 else "***"
        log.info(
            "signup bot enabled (token %s, admin_chat_id=%s)",
            masked, ADMIN_TG_CHAT_ID,
        )
    else:
        log.info("signup bot disabled — set SIGNUP_TELEGRAM_BOT_TOKEN to enable")
    try:
        yield
    finally:
        if _calendar_refresher is not None:
            await _calendar_refresher.stop()
            _calendar_refresher = None
        if _expiry_notifier is not None:
            _expiry_notifier.cancel()
            try:
                await _expiry_notifier
            except (asyncio.CancelledError, Exception):
                pass
            _expiry_notifier = None
        if _signup_bot is not None:
            await _signup_bot.stop()
            _signup_bot = None


async def _subscription_expiry_loop() -> None:
    """Background task: every 15 minutes, email users who just expired."""
    interval = int(os.environ.get("SUBSCRIPTION_EXPIRY_SCAN_S", "900"))
    while True:
        try:
            _send_expiry_emails()
        except Exception:
            log.exception("subscription-expiry scan failed")
        await asyncio.sleep(interval)


def _send_expiry_emails() -> None:
    """Pull expired-unnotified users, fire one email each, mark notified."""
    from src.api.mailer import send_subscription_expired_email
    rows = user_store.list_expired_unnotified()
    for u in rows:
        if not u.email:
            user_store.mark_notified(u.username)  # nothing to send to
            continue
        try:
            send_subscription_expired_email(to=u.email, ad_id=u.username)
            user_store.mark_notified(u.username)
        except Exception:
            log.exception(
                "subscription-expired email failed for %s; will retry next scan",
                u.username,
            )


app = FastAPI(title="Forex-EA Control API", version="0.3.0", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class _BotState:
    running: bool = False
    last_heartbeat: datetime | None = None
    balance: float = 10_000.0


state = _BotState()
_DB = Path("data/trades.db")
journal = TradeJournal(_DB)
toggle_store = StrategyToggleStore(_DB)
toggle_store.initialize_defaults({
    name: DEFAULT_STRATEGY_FLAGS.get(name, False) for name in STRATEGY_REGISTRY
})
user_store = UserStore(_DB)
subscription_request_store = SubscriptionRequestStore(_DB)
signal_feed = SignalFeed(_DB)
ea_account_store = EAAccountReportStore(_DB)
bot_control_store = BotControlStore(_DB)
SIGNUP_BOT_TOKEN = os.environ.get("SIGNUP_TELEGRAM_BOT_TOKEN", "").strip() or None
ADMIN_TG_CHAT_ID = (
    int(os.environ["TELEGRAM_CHAT_ID"])
    if os.environ.get("TELEGRAM_CHAT_ID", "").strip().lstrip("-").isdigit()
    else None
)


def current_user(user: dict = Depends(_current_user_jwt)) -> dict:
    """Wraps the JWT-only current_user with a subscription-expiry check.

    Any request whose token decodes correctly but whose subscription has
    lapsed is rejected with 401 + a clear "subscription expired" message,
    so the dashboard's existing 401-handler dumps them back to login.
    """
    sub = user.get("username") or user.get("sub") or ""
    if sub and user_store.is_expired(sub):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "subscription expired — contact admin to renew",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def ea_caller(authorization: str = Header(default="")) -> str:
    """Dependency for the EA-facing endpoints. Resolves a Bearer EA
    API key to a username, applies the same expired-subscription gate
    as current_user. Returns the username string.
    """
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    key = authorization.split(" ", 1)[1].strip()
    username = user_store.get_username_by_ea_key(key)
    if username is None:
        raise HTTPException(401, "invalid EA API key")
    if user_store.is_expired(username):
        raise HTTPException(
            403, "subscription expired — contact admin to renew",
        )
    return username


def require_admin(user: dict = Depends(current_user)) -> dict:
    """Same as auth.require_admin but goes through our expiry-aware
    current_user so an expired admin token (shouldn't happen — admin
    has no expiry — but defensive) gets 401, not 403.
    """
    if user.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin privileges required")
    return user
broker_status_store = BrokerStatusStore(_DB)
pending_orders_store = PendingOrderStore(_DB)
calendar_store = EventStore(_DB)
calendar_policy = BlackoutPolicy.from_env()
calendar_checker = BlackoutChecker(calendar_store, calendar_policy)
regime_store = RegimeStore(_DB)
correlation_store = CorrelationStore(_DB)
drift_baseline_store = BaselineStore(_DB)
drift_monitor = DriftMonitor(_DB, drift_baseline_store, DriftConfig.from_env())
fill_store = FillStore(_DB)
allocation_store = AllocationStore(_DB)
explanation_store = TradeExplanationStore(_DB)
heartbeat_store = HeartbeatStore(_DB)
propfirm_store = PropFirmStore(_DB)
narrative_store = NarrativeStore(_DB)
path_store = PathStore(_DB)
voice_kill_flag = KillSwitchFlag(_DB)
voice_log_store = VoiceLogStore(_DB)
# Refresher is created on startup so tests that import this module without a
# running event loop don't need to deal with asyncio tasks.
_calendar_refresher: CalendarRefresher | None = None
_expiry_notifier: asyncio.Task | None = None
_signup_bot: TelegramSignupBot | None = None
# Lazy — needs AUTH_SECRET and that check belongs on first real use, not import.
_broker_config_store: BrokerConfigStore | None = None
_totp_store: TOTPStore | None = None


def _broker_store() -> BrokerConfigStore:
    global _broker_config_store
    if _broker_config_store is None:
        _broker_config_store = BrokerConfigStore(_DB, secret=_secret())
    return _broker_config_store


def _totp() -> TOTPStore:
    global _totp_store
    if _totp_store is None:
        _totp_store = TOTPStore(_DB, secret=_secret())
    return _totp_store


def require_2fa(
    request: Request,
    user: dict = Depends(current_user),
) -> dict:
    """Gate destructive operations behind a fresh TOTP code when the caller
    has 2FA enabled. Opt-in per user — accounts without an active secret
    pass through untouched, so existing flows keep working until an
    operator chooses to enroll.
    """
    store = _totp()
    if not store.is_enabled(user["username"]):
        return user
    code = (request.headers.get("X-2FA-Code") or "").strip()
    if not code:
        raise HTTPException(401, "2FA code required (X-2FA-Code header)")
    secret = store.get_active_secret(user["username"])
    if secret is None or not verify_code(secret, code):
        raise HTTPException(401, "invalid 2FA code")
    return user


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: int
    username: str
    role: Literal["admin", "user"]


class UserResponse(BaseModel):
    username: str
    role: Literal["admin", "user"]
    email: str | None = None
    created_at: str
    password_set: bool
    expires_at: str | None = None
    expired: bool = False
    phone_number: str | None = None
    display_name: str | None = None


class AssignUserRequest(BaseModel):
    ad_id: str = Field(min_length=1, max_length=32)
    email: str = Field(min_length=3, max_length=254)
    # Subscription window. Codes are kept short so the dropdown is
    # readable: '5h', '1w', '2w', '1m', '2m', '3m'.
    duration: str = Field(default="1m", pattern="^(5h|1w|2w|1m|2m|3m)$")


class AssignUserResponse(BaseModel):
    ad_id: str
    email: str
    setup_expires_at: int
    setup_url: str | None = None  # populated in dev mode so admin can copy the link
    subscription_expires_at: str | None = None  # ISO; None for unlimited


class ExtendSubscriptionRequest(BaseModel):
    duration: str = Field(pattern="^(5h|1w|2w|1m|2m|3m)$")


class SubscriptionRequestResponse(BaseModel):
    id: int
    telegram_chat_id: int
    telegram_username: str | None
    telegram_first_name: str | None
    duration: str
    email: str
    phone_number: str | None = None
    status: str
    created_at: str
    decided_at: str | None
    decided_by: str | None
    assigned_ad_id: str | None
    rejection_reason: str | None


class ApproveRequestRequest(BaseModel):
    # Allow override of duration on approval — the admin might want
    # to give a longer window than the user requested. Defaults to
    # whatever the user picked.
    duration: str | None = Field(default=None, pattern="^(5h|1w|2w|1m|2m|3m)$")
    # Optional: pin a specific AD-ID. Otherwise the next pool ID is used.
    ad_id: str | None = Field(default=None, max_length=32)
    # How to deliver the setup link. "telegram" uses the bot DM only,
    # "email" uses the mailer only, "both" tries both (default).
    # Telegram-only is useful when the email path is unreachable.
    delivery: str = Field(default="telegram", pattern="^(telegram|email|both)$")


class RejectRequestRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


class SetupPasswordRequest(BaseModel):
    password: str = Field(min_length=12, max_length=72)


class SetupClaimsResponse(BaseModel):
    ad_id: str
    email: str
    expires_at: int
    phone_number: str | None = None


class PoolResponse(BaseModel):
    unclaimed: list[str]
    size: int


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=12, max_length=72)


class StatusResponse(BaseModel):
    running: bool
    mt5_connected: bool
    last_heartbeat: datetime | None
    open_positions: int


class AccountResponse(BaseModel):
    balance: float
    equity: float
    open_positions: int
    daily_pnl: float = 0.0


class StrategyResponse(BaseModel):
    name: str
    enabled: bool
    mode: str = "execute"  # 'execute' (bot trades) | 'signal' (alerts only)


class StrategyModeRequest(BaseModel):
    mode: str = Field(pattern="^(execute|signal)$")


class BrokerConfigRequest(BaseModel):
    broker: str = Field(min_length=1, max_length=32)
    login: int = Field(gt=0)
    # Empty password means "reuse the one already stored for this broker" —
    # lets the dashboard re-test after save without forcing the user to
    # retype the password every time.
    password: str = Field(default="", max_length=256)
    server: str = Field(min_length=1, max_length=128)
    mt5_path: str = Field(default="", max_length=512)


class BrokerConfigResponse(BaseModel):
    broker: str
    login: int
    server: str
    mt5_path: str
    password_set: bool
    password_fingerprint: str
    updated_at: str


class BrokerTestRequest(BrokerConfigRequest):
    """Same fields as save, but doesn't persist — just attempts a connection."""


class BrokerTestResponse(BaseModel):
    ok: bool
    error: str | None = None
    account: dict | None = None


class BrokerStatusResponse(BaseModel):
    connected: bool
    broker: str | None = None
    server: str | None = None
    login: int | None = None
    account_info: dict | None = None
    last_error: str | None = None
    updated_at: datetime | None = None
    stale_s: float | None = None


class TradeResponse(BaseModel):
    id: int
    symbol: str
    side: Literal["BUY", "SELL"]
    entry_price: float
    exit_price: float | None
    pnl: float
    opened_at: datetime
    closed_at: datetime | None
    lot_size: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    strategy: str | None = None
    broker_ticket: int | None = None
    close_reason: str | None = None


def _open_positions(since_iso: str | None = None) -> int:
    rows = journal.recent(limit=200, since_iso=since_iso)
    return sum(1 for r in rows if r.get("status") == "OPEN")


def _resolve_password(body: BrokerConfigRequest, username: str) -> str:
    """Return the password to use: the one in the request, or the user's saved one.

    After the UI saves creds the password input is cleared, so a subsequent
    Test / Save click arrives with password="". In that case we fall back to
    the stored password for this specific user. If nothing is saved yet, refuse —
    first save must include the password.
    """
    if body.password:
        return body.password
    existing = _broker_store().get_decrypted(username)
    if existing is None:
        raise HTTPException(400, "password required")
    return existing.password


@app.post("/auth/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    request: Request,
    limiter: LoginRateLimiter = Depends(rate_limiter),
) -> LoginResponse:
    ip = client_ip(request)
    limiter.check(ip)
    role = authenticate(user_store, body.username, body.password)
    if role is None:
        limiter.record(ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    limiter.reset(ip)
    token, exp = create_token(body.username, role=role)
    return LoginResponse(
        access_token=token, expires_at=exp, username=body.username, role=role,
    )


@app.get("/auth/me")
def me(user: dict[str, str] = Depends(current_user)) -> dict[str, str]:
    return {"username": user["username"], "role": user["role"]}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/status", response_model=StatusResponse)
def get_status(_user: dict = Depends(current_user)) -> StatusResponse:
    bs = broker_status_store.read()
    # "running" reflects the operator's intent stored in bot_control —
    # the bot process polls that flag and pauses when False. We also
    # require a recent heartbeat: should_run=True but no heartbeat in
    # the last 3 minutes means the bot crashed / is wedged, which the
    # dashboard should show as not running (so the user sees a real
    # problem rather than a false-positive green dot).
    bot_hb = heartbeat_store.read("bot")
    should_run = bot_control_store.should_run()
    bot_last_hb = state.last_heartbeat
    hb_fresh = False
    if bot_hb is not None:
        bot_last_hb = bot_hb.last_tick_at
        hb_fresh = (datetime.now(timezone.utc) - bot_hb.last_tick_at).total_seconds() < 180
    bot_running = should_run and hb_fresh
    return StatusResponse(
        running=bot_running,
        mt5_connected=bool(bs.connected) if bs else False,
        last_heartbeat=bot_last_hb,
        open_positions=_open_positions(),
    )


@app.get("/account", response_model=AccountResponse)
def account(user: dict = Depends(current_user)) -> AccountResponse:
    # Non-admin operators run MT5 on their own machine. Their EA POSTs
    # an account snapshot every 60s — prefer that over admin's MT5 so
    # the dashboard shows the operator's actual numbers, not the master.
    if user.get("role") != "admin":
        username = user.get("username") or user.get("sub") or ""
        report = ea_account_store.get(username) if username else None
        if report is not None:
            balance = float(report.balance or 0.0)
            equity = float(report.equity or balance)
            floating = equity - balance
            since_iso = (
                report.first_seen_at.isoformat()
                if report.first_seen_at else None
            )
            return AccountResponse(
                balance=balance,
                equity=equity,
                open_positions=_open_positions(since_iso=since_iso),
                daily_pnl=floating,
            )
        # No EA snapshot yet: return zeros so the user doesn't see
        # admin's account while their EA is still booting.
        return AccountResponse(
            balance=0.0, equity=0.0, open_positions=0, daily_pnl=0.0,
        )

    today = journal.summary_today()
    status = broker_status_store.read()
    info = status.account_info if status and status.connected else None
    balance = info["balance"] if info and "balance" in info else state.balance
    equity = info["equity"] if info and "equity" in info else balance + today["pnl"]
    # Today P&L = realized closed-trade pnl from the journal + floating
    # unrealized from currently open positions. The bot tick keeps
    # broker_status_store fresh with equity, so floating = equity - balance.
    # Falls back to realized-only when we don't have a live snapshot yet
    # (cold start, MT5 disconnected).
    realized = float(today.get("pnl") or 0.0)
    floating = 0.0
    if info and "balance" in info and "equity" in info:
        try:
            floating = float(info["equity"]) - float(info["balance"])
        except (TypeError, ValueError):
            floating = 0.0
    return AccountResponse(
        balance=balance,
        equity=equity,
        open_positions=_open_positions(),
        daily_pnl=realized + floating,
    )


@app.get("/strategies", response_model=list[StrategyResponse])
def list_strategies(_user: dict = Depends(current_user)) -> list[StrategyResponse]:
    return [
        StrategyResponse(name=s["name"], enabled=s["enabled"], mode=s["mode"])
        for s in toggle_store.list_full()
    ]


@app.post("/strategies/{name}/mode", response_model=StrategyResponse)
def set_strategy_mode(
    name: str,
    body: StrategyModeRequest,
    _admin: dict = Depends(require_admin),
) -> StrategyResponse:
    try:
        toggle_store.set_mode(name, body.mode)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown strategy: {name}")
    full = toggle_store.get_full(name)
    return StrategyResponse(
        name=full["name"], enabled=full["enabled"], mode=full["mode"],
    )


@app.post("/strategies/{name}/toggle", response_model=StrategyResponse)
def toggle_strategy(name: str, _user: dict = Depends(require_2fa)) -> StrategyResponse:
    try:
        enabled = toggle_store.toggle(name)
    except KeyError:
        raise HTTPException(404, f"strategy '{name}' not found") from None
    full = toggle_store.get_full(name)
    return StrategyResponse(
        name=name,
        enabled=enabled,
        mode=full["mode"] if full else "execute",
    )


@app.get("/trades", response_model=list[TradeResponse])
def trades(limit: int = 20, user: dict = Depends(current_user)) -> list[TradeResponse]:
    # Non-admin operators only see trades from the moment their EA first
    # checked in. Before that, the master trades aren't "theirs" — the
    # EA wasn't online to copy them.
    since_iso: str | None = None
    if user.get("role") != "admin":
        username = user.get("username") or user.get("sub") or ""
        report = ea_account_store.get(username) if username else None
        if report is None or report.first_seen_at is None:
            return []
        since_iso = report.first_seen_at.isoformat()
    rows = journal.recent(limit=limit, since_iso=since_iso)
    out: list[TradeResponse] = []
    for r in rows:
        sl = r.get("stop_loss")
        tp = r.get("take_profit")
        out.append(TradeResponse(
            id=r["id"],
            symbol=r["symbol"],
            side=r["side"],
            entry_price=r["entry_price"],
            exit_price=r["exit_price"],
            pnl=r["pnl"] or 0.0,
            opened_at=datetime.fromisoformat(r["opened_at"]),
            closed_at=datetime.fromisoformat(r["closed_at"]) if r["closed_at"] else None,
            lot_size=float(r.get("lot_size") or 0.0),
            # Journal stores 0.0 for "no SL/TP" historically — translate
            # that to null on the wire so the dashboard renders "—"
            # instead of a nonsense 0.00000.
            stop_loss=(float(sl) if sl else None),
            take_profit=(float(tp) if tp else None),
            strategy=r.get("strategy"),
            broker_ticket=r.get("broker_ticket"),
            close_reason=r.get("close_reason"),
        ))
    return out


@app.post("/bot/start")
def start_bot(user: dict = Depends(require_2fa)) -> dict[str, str]:
    state.running = True
    state.last_heartbeat = datetime.utcnow()
    bot_control_store.set(True, by=user.get("username"))
    return {"status": "started"}


@app.post("/bot/stop")
def stop_bot(user: dict = Depends(require_2fa)) -> dict[str, str]:
    state.running = False
    bot_control_store.set(False, by=user.get("username"))
    return {"status": "stopped"}


# ---------- Broker management ----------

@app.get("/brokers")
def list_broker_presets(_user: dict = Depends(current_user)) -> list[dict]:
    return broker_presets.as_dicts()


@app.get("/broker/config", response_model=BrokerConfigResponse | None)
def get_broker_config(user: dict = Depends(current_user)) -> BrokerConfigResponse | None:
    masked = _broker_store().get_masked(user["username"])
    if masked is None:
        return None
    return BrokerConfigResponse(**masked)


@app.put("/broker/config", response_model=BrokerConfigResponse)
def save_broker_config(
    body: BrokerConfigRequest,
    user: dict = Depends(require_2fa),
) -> BrokerConfigResponse:
    if body.broker not in broker_presets.PRESET_BY_ID:
        raise HTTPException(400, f"unknown broker '{body.broker}'")
    username = user["username"]
    password = _resolve_password(body, username)
    store = _broker_store()
    store.save(username, BrokerConfig(
        broker=body.broker,
        login=body.login,
        password=password,
        server=body.server,
        mt5_path=body.mt5_path,
    ))
    return BrokerConfigResponse(**store.get_masked(username))


@app.delete("/broker/config")
def clear_broker_config(user: dict = Depends(require_2fa)) -> dict[str, bool]:
    removed = _broker_store().clear(user["username"])
    return {"removed": removed}


@app.post("/broker/test", response_model=BrokerTestResponse)
def test_broker(
    body: BrokerTestRequest,
    user: dict = Depends(current_user),
) -> BrokerTestResponse:
    """Open a temporary MT5 connection with these creds, fetch account_info, disconnect.

    Returns ok=False on any failure (MT5 not installed, bad creds, wrong server,
    terminal not running). Never raises — callers expect a structured response.
    """
    try:
        from src.connection.mt5_client import MT5Client
    except Exception as e:
        return BrokerTestResponse(ok=False, error=f"MT5 client unavailable: {e}")
    password = _resolve_password(body, user["username"])
    try:
        client = MT5Client(
            login=body.login, password=password,
            server=body.server, path=body.mt5_path or None,
        )
    except RuntimeError as e:
        # MetaTrader5 package not installed (macOS / Linux). The UI treats this
        # as "can't test here — save and test on the Windows VPS".
        return BrokerTestResponse(ok=False, error=str(e))
    try:
        client.connect()
        info = client.account_info()
        account_info = {
            "balance": info.balance,
            "equity": info.equity,
            "currency": info.currency,
            "leverage": info.leverage,
        }
        broker_status_store.write(
            connected=True,
            broker=body.broker,
            server=info.server,
            login=info.login,
            account_info=account_info,
        )
        return BrokerTestResponse(ok=True, account={
            "login": info.login,
            "server": info.server,
            **account_info,
        })
    except Exception as e:
        broker_status_store.write(
            connected=False,
            broker=body.broker,
            server=body.server,
            login=body.login,
            last_error=str(e),
        )
        return BrokerTestResponse(ok=False, error=str(e))
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


@app.get("/orders/pending")
def list_pending_orders(_user: dict = Depends(current_user)) -> list[dict]:
    """Pending limit/stop orders the bot has snapshotted from MT5.

    Empty if the bot is in mock mode or hasn't ticked yet. Each item:
    `{ ticket, symbol, order_type, price, volume, sl, tp, comment, placed_at }`.
    """
    rows = pending_orders_store.read()
    return [
        {
            "ticket": r.ticket,
            "symbol": r.symbol,
            "order_type": r.order_type,
            "price": r.price,
            "volume": r.volume,
            "sl": r.sl,
            "tp": r.tp,
            "comment": r.comment,
            "placed_at": r.placed_at.isoformat(),
        }
        for r in rows
    ]


# ---------- EA copy-trader endpoints ----------

class EAConfigResponse(BaseModel):
    """Config the user pastes into the AntiGreedCopier EA."""
    api_base_url: str
    api_key: str
    ad_id: str
    instructions_url: str | None = None


class EAAccountReportRequest(BaseModel):
    """Account snapshot POSTed by AntiGreedCopier every 60s."""
    balance: float | None = None
    equity: float | None = None
    margin: float | None = None
    free_margin: float | None = None
    login: int | None = None
    server: str | None = None
    broker: str | None = None
    currency: str | None = None


@app.get("/me/ea-config", response_model=EAConfigResponse)
def my_ea_config(user: dict = Depends(current_user)) -> EAConfigResponse:
    """Return the current user's copy-trading EA config — base URL,
    API key, AD-ID. Generates the key on first call if missing.
    Admin doesn't need this (they're the source, not the copier),
    but the endpoint is open to all users for symmetry.
    """
    username = user.get("username") or user.get("sub") or ""
    if not username:
        raise HTTPException(400, "username missing from token")
    try:
        key = user_store.ensure_ea_api_key(username)
    except KeyError:
        raise HTTPException(404, "user not found") from None
    base = os.environ.get("PUBLIC_BASE_URL") or "http://163.5.178.251:8000"
    return EAConfigResponse(
        api_base_url=base.rstrip("/"),
        api_key=key,
        ad_id=username,
    )


@app.post("/me/ea-config/rotate", response_model=EAConfigResponse)
def rotate_my_ea_config(user: dict = Depends(current_user)) -> EAConfigResponse:
    """Force a new EA API key — invalidates the old one. The user's
    installed EA stops working until they paste the new key. Use this
    after a leak / lost device.
    """
    username = user.get("username") or user.get("sub") or ""
    if not username:
        raise HTTPException(400, "username missing from token")
    try:
        key = user_store.rotate_ea_api_key(username)
    except KeyError:
        raise HTTPException(404, "user not found") from None
    base = os.environ.get("PUBLIC_BASE_URL") or "http://163.5.178.251:8000"
    return EAConfigResponse(
        api_base_url=base.rstrip("/"), api_key=key, ad_id=username,
    )


@app.get("/signals/feed")
def signal_feed_endpoint(
    since: str | None = None,
    limit: int = 100,
    username: str = Depends(ea_caller),
) -> dict:
    """Polled by the AntiGreedCopier EA. Returns OPEN/CLOSE events
    with a timestamp newer than ``since`` (ISO-8601). On first poll
    (no ``since``) the feed bookmarks the latest known event and
    returns an empty list — the EA only acts on trades that fire
    *after* it boots, never replays historical opens.
    """
    if limit <= 0 or limit > 500:
        limit = 100
    try:
        events = signal_feed.events_since(since, limit=limit)
    except Exception:
        log.exception("signal feed query failed")
        raise HTTPException(500, "feed query failed") from None
    # Always include a bookmark — the EA stores it and sends it back.
    # On the cold-start (since=None) path we still return the latest
    # known timestamp so the EA can begin polling forward from there.
    if not since:
        try:
            with sqlite3.connect(_DB) as c:
                row = c.execute(
                    "SELECT COALESCE(MAX(ts), '1970-01-01T00:00:00+00:00') AS mx "
                    "FROM ("
                    "  SELECT opened_at AS ts FROM trades "
                    "  UNION ALL "
                    "  SELECT closed_at AS ts FROM trades WHERE closed_at IS NOT NULL"
                    ")"
                ).fetchone()
            bookmark = row[0]
        except Exception:
            bookmark = "1970-01-01T00:00:00+00:00"
    else:
        bookmark = events[-1].ts if events else since
    return {
        "user": username,
        "bookmark": bookmark,
        "events": [
            {
                "type": e.event_type,
                "trade_id": e.trade_id,
                "ts": e.ts,
                "symbol": e.symbol,
                "side": e.side,
                "lot_size": e.lot_size,
                "price": e.price,
                "stop_loss": e.stop_loss,
                "take_profit": e.take_profit,
                "strategy": e.strategy,
                "broker_ticket": e.broker_ticket,
            }
            for e in events
        ],
    }


@app.post("/me/ea-account")
def report_ea_account(
    body: EAAccountReportRequest,
    username: str = Depends(ea_caller),
) -> dict:
    """Called by AntiGreedCopier every 60s with the operator's local
    MT5 account snapshot. Stored per-user; the dashboard's /account
    endpoint reads it back so non-admins see *their* numbers, not
    admin's. EA-key authenticated.
    """
    ea_account_store.upsert(
        username,
        balance=body.balance,
        equity=body.equity,
        margin=body.margin,
        free_margin=body.free_margin,
        login=body.login,
        server=body.server,
        broker=body.broker,
        currency=body.currency,
    )
    return {"ok": True}


@app.get("/broker/status", response_model=BrokerStatusResponse)
def get_broker_status(user: dict = Depends(current_user)) -> BrokerStatusResponse:
    # Non-admin: project the operator's own EA-reported snapshot as the
    # "broker status". connected = EA has POSTed in the last 5 minutes.
    if user.get("role") != "admin":
        username = user.get("username") or user.get("sub") or ""
        report = ea_account_store.get(username) if username else None
        if report is None:
            return BrokerStatusResponse(connected=False)
        age = (datetime.now(report.updated_at.tzinfo) - report.updated_at).total_seconds()
        info = {
            "balance": report.balance,
            "equity": report.equity,
            "margin": report.margin,
            "free_margin": report.free_margin,
            "currency": report.currency,
        }
        return BrokerStatusResponse(
            connected=age < 300,
            broker=report.broker,
            server=report.server,
            login=report.login,
            account_info=info,
            last_error=None,
            updated_at=report.updated_at,
            stale_s=age,
        )

    s = broker_status_store.read()
    if s is None:
        return BrokerStatusResponse(connected=False)
    age = (datetime.now(s.updated_at.tzinfo) - s.updated_at).total_seconds()
    return BrokerStatusResponse(
        connected=s.connected,
        broker=s.broker,
        server=s.server,
        login=s.login,
        account_info=s.account_info,
        last_error=s.last_error,
        updated_at=s.updated_at,
        stale_s=age,
    )


# ---------- User management (admin only) ----------

def _to_response(u) -> UserResponse:
    # Hide the synthetic placeholder email we generate for phone-based
    # signups so the dashboard doesn't show "telegram-12345@no-email.local".
    visible_email = u.email
    if visible_email and visible_email.endswith("@no-email.local"):
        visible_email = None
    return UserResponse(
        username=u.username, role=u.role, email=visible_email,
        created_at=u.created_at, password_set=u.password_set,
        expires_at=u.expires_at,
        expired=u.expired,
        phone_number=getattr(u, "phone_number", None),
        display_name=getattr(u, "display_name", None),
    )


@app.get("/users", response_model=list[UserResponse])
def list_users(_admin: dict = Depends(require_admin)) -> list[UserResponse]:
    return [_to_response(u) for u in user_store.list_users()]


@app.get("/users/pool", response_model=PoolResponse)
def user_pool(_admin: dict = Depends(require_admin)) -> PoolResponse:
    ids = user_store.unclaimed_pool()
    return PoolResponse(unclaimed=ids, size=len(ids))


@app.post("/users/pool/refill", response_model=PoolResponse)
def refill_pool(
    target: int = 100,
    _admin: dict = Depends(require_admin),
    _twofa: dict = Depends(require_2fa),
) -> PoolResponse:
    if target < 1 or target > 1000:
        raise HTTPException(400, "target must be between 1 and 1000")
    user_store.refill_pool(target)
    ids = user_store.unclaimed_pool()
    return PoolResponse(unclaimed=ids, size=len(ids))


@app.post("/users/assign", response_model=AssignUserResponse, status_code=status.HTTP_201_CREATED)
def assign_user(
    body: AssignUserRequest,
    _admin: dict = Depends(require_admin),
    _twofa: dict = Depends(require_2fa),
) -> AssignUserResponse:
    """Claim an AD-ID + email setup link. The recipient picks their own password."""
    if body.ad_id == ADMIN_AD_ID or not is_user_ad_id(body.ad_id):
        raise HTTPException(400, "invalid AD-ID")
    if "@" not in body.email or "." not in body.email.split("@")[-1]:
        raise HTTPException(400, "invalid email address")
    try:
        duration = parse_duration(body.duration)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    try:
        expires = user_store.assign(body.ad_id, body.email, duration=duration)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return _issue_setup_link(body.ad_id, body.email, subscription_expires_at=expires)


@app.post("/users/{username}/extend", response_model=UserResponse)
def extend_user_subscription(
    username: str,
    body: ExtendSubscriptionRequest,
    _admin: dict = Depends(require_admin),
    _twofa: dict = Depends(require_2fa),
) -> UserResponse:
    """Push the subscription forward by the requested duration. If the
    user is currently expired, the new window starts from now (so they
    don't lose any of the renewal). Clears the expired-notified flag so
    a future expiry triggers a fresh email.
    """
    if username == ADMIN_AD_ID:
        raise HTTPException(400, "admin has no subscription to extend")
    try:
        duration = parse_duration(body.duration)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    try:
        user_store.extend(username, duration)
    except KeyError:
        raise HTTPException(404, "user not found") from None
    full = next(
        (u for u in user_store.list_users() if u.username == username),
        None,
    )
    if full is None:
        raise HTTPException(404, "user not found")
    return _to_response(full)


def _request_to_response(r: SubscriptionRequest) -> SubscriptionRequestResponse:
    return SubscriptionRequestResponse(
        id=r.id,
        telegram_chat_id=r.telegram_chat_id,
        telegram_username=r.telegram_username,
        telegram_first_name=r.telegram_first_name,
        duration=r.duration,
        email=r.email,
        phone_number=r.phone_number,
        status=r.status,
        created_at=r.created_at,
        decided_at=r.decided_at,
        decided_by=r.decided_by,
        assigned_ad_id=r.assigned_ad_id,
        rejection_reason=r.rejection_reason,
    )


@app.get("/subscription-requests", response_model=list[SubscriptionRequestResponse])
def list_subscription_requests(
    pending_only: bool = True,
    _admin: dict = Depends(require_admin),
) -> list[SubscriptionRequestResponse]:
    """Telegram-bot signup requests visible to admins. Pass
    ?pending_only=false to also see approved/rejected history.
    """
    rows = (subscription_request_store.list_pending() if pending_only
            else subscription_request_store.list_recent(limit=100))
    return [_request_to_response(r) for r in rows]


@app.post(
    "/subscription-requests/{request_id}/approve",
    response_model=AssignUserResponse,
)
def approve_subscription_request(
    request_id: int,
    body: ApproveRequestRequest,
    admin: dict = Depends(require_admin),
    _twofa: dict = Depends(require_2fa),
) -> AssignUserResponse:
    """Assign an AD-ID to a Telegram signup request.

    Picks the next pool ID (or the one the admin specified), seeds the
    user with the requested duration, fires the setup-link email, and
    DMs the user via the Telegram signup bot to tell them to check
    their inbox.
    """
    req = subscription_request_store.get(request_id)
    if req is None:
        raise HTTPException(404, "request not found")
    if req.status != "pending":
        raise HTTPException(409, f"request is already {req.status}")

    # Pick AD-ID — explicit override or next from the pool.
    ad_id = body.ad_id
    if ad_id:
        if ad_id == ADMIN_AD_ID or not is_user_ad_id(ad_id):
            raise HTTPException(400, "invalid AD-ID")
    else:
        pool = user_store.unclaimed_pool()
        if not pool:
            raise HTTPException(409, "AD-ID pool empty — refill first")
        ad_id = pool[0]

    duration_code = body.duration or req.duration
    try:
        duration = parse_duration(duration_code)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None

    # Phone-based signups have an empty email field — fall back to a
    # synthetic placeholder so the user_store row insert doesn't fail
    # its NOT NULL on email. Real email can be added later if needed —
    # the dashboard hides the placeholder anyway.
    contact_email = req.email or f"telegram-{req.telegram_chat_id}@no-email.local"
    try:
        user_store.assign(
            ad_id, contact_email, duration=duration,
            phone_number=req.phone_number,
            display_name=req.telegram_first_name or req.telegram_username,
        )
    except ValueError as e:
        raise HTTPException(409, str(e)) from None

    # Mark the request approved before sending anything so a flaky
    # mailer or Telegram outage doesn't leave us with an approved row
    # that wasn't notified.
    subscription_request_store.mark_approved(
        request_id, admin=admin.get("username", "admin"), assigned_ad_id=ad_id,
    )

    # Mint the setup token regardless of delivery method — the URL
    # itself is short-lived and harmless to generate.
    token, exp, url = create_setup_token(ad_id, contact_email)
    hours = SETUP_TTL_S // 3600

    delivery = body.delivery or "telegram"
    telegram_ok = False
    email_ok = False
    last_email_error: str | None = None

    if delivery in ("telegram", "both"):
        try:
            telegram_ok = send_approval_dm_with_link(
                SIGNUP_BOT_TOKEN, req.telegram_chat_id, ad_id, duration_code,
                setup_url=url, expires_hours=hours,
            )
        except Exception:
            log.exception(
                "approval Telegram DM failed for chat %s", req.telegram_chat_id,
            )

    if delivery in ("email", "both"):
        if not req.email:
            last_email_error = "no email on record (phone-based signup)"
        else:
            try:
                send_setup_email(
                    to=req.email, ad_id=ad_id, setup_url=url, expires_hours=hours,
                )
                email_ok = True
            except Exception as e:
                last_email_error = str(e)
                log.exception("approval email failed for %s", req.email)

    # If telegram-only and the DM failed, that's the only delivery path
    # — fail loud so the admin knows. Same for email-only.
    if delivery == "telegram" and not telegram_ok:
        raise HTTPException(502, "Telegram DM failed — bot may be offline")
    if delivery == "email" and not email_ok:
        raise HTTPException(502, f"email delivery failed: {last_email_error}")
    # 'both' is forgiving — if at least one succeeded we report success
    if delivery == "both" and not (telegram_ok or email_ok):
        raise HTTPException(
            502, f"both delivery paths failed (email: {last_email_error})",
        )

    from src.api.mailer import mailer_configured
    return AssignUserResponse(
        ad_id=ad_id, email=req.email, setup_expires_at=exp,
        # Hand back the URL when no mailer is configured OR when the
        # admin chose telegram-only — useful for copy/paste fallback.
        setup_url=url if (delivery == "telegram" or not mailer_configured()) else None,
        subscription_expires_at=user_store.get_expires_at(ad_id),
    )


@app.post(
    "/subscription-requests/{request_id}/reject",
    response_model=SubscriptionRequestResponse,
)
def reject_subscription_request(
    request_id: int,
    body: RejectRequestRequest,
    admin: dict = Depends(require_admin),
    _twofa: dict = Depends(require_2fa),
) -> SubscriptionRequestResponse:
    """Decline a pending signup request. The reason is sent to the
    user via Telegram so they know what to do next.
    """
    req = subscription_request_store.get(request_id)
    if req is None:
        raise HTTPException(404, "request not found")
    if req.status != "pending":
        raise HTTPException(409, f"request is already {req.status}")
    updated = subscription_request_store.mark_rejected(
        request_id, admin=admin.get("username", "admin"), reason=body.reason,
    )
    try:
        send_rejection_dm(SIGNUP_BOT_TOKEN, req.telegram_chat_id, body.reason)
    except Exception:
        log.exception("rejection Telegram DM failed for chat %s", req.telegram_chat_id)
    return _request_to_response(updated or req)


@app.post("/users/{username}/resend", response_model=AssignUserResponse)
def resend_setup_link(
    username: str,
    _admin: dict = Depends(require_admin),
) -> AssignUserResponse:
    """Email a fresh setup link to a pending operator who lost the first one."""
    if not user_store.exists(username):
        raise HTTPException(404, "user not found")
    email = user_store.get_email(username)
    if not email:
        raise HTTPException(400, "no email on file for this user")
    if user_store.get_hash(username):
        raise HTTPException(409, "user already has a password set — use reset-password instead")
    return _issue_setup_link(username, email)


def _issue_setup_link(
    ad_id: str, email: str, *,
    subscription_expires_at: str | None = None,
) -> AssignUserResponse:
    """Mint a fresh setup JWT, try to email it, surface the URL in dev mode."""
    from src.api.mailer import mailer_configured

    token, exp, url = create_setup_token(ad_id, email)
    hours = SETUP_TTL_S // 3600
    if subscription_expires_at is None:
        subscription_expires_at = user_store.get_expires_at(ad_id)
    try:
        send_setup_email(to=email, ad_id=ad_id, setup_url=url, expires_hours=hours)
    except Exception as e:
        # The AD-ID is already assigned — surface the failure so the admin
        # knows to fix the mailer or send the link manually.
        raise HTTPException(502, f"email delivery failed: {e}") from None
    return AssignUserResponse(
        ad_id=ad_id, email=email, setup_expires_at=exp,
        # No mailer wired up → email wasn't really sent — hand back the URL
        # so the admin can copy it to the recipient.
        setup_url=None if mailer_configured() else url,
        subscription_expires_at=subscription_expires_at,
    )


@app.delete("/users/{username}")
def delete_user(
    username: str,
    admin: dict = Depends(require_admin),
    _twofa: dict = Depends(require_2fa),
) -> dict[str, bool]:
    if username == admin["username"]:
        raise HTTPException(400, "cannot delete your own account")
    try:
        removed = user_store.delete(username)
    except LastAdminError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
    if not removed:
        raise HTTPException(404, "user not found")
    return {"removed": True}


@app.post("/users/{username}/reset-password")
def reset_user_password(
    username: str,
    body: ResetPasswordRequest,
    _admin: dict = Depends(require_admin),
    _twofa: dict = Depends(require_2fa),
) -> dict[str, bool]:
    if not user_store.exists(username):
        raise HTTPException(404, "user not found")
    try:
        pw_hash = hash_password(body.password)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    user_store.set_password(username, pw_hash)
    return {"updated": True}


# ---------- Public setup flow (no auth: the JWT is the auth) ----------

@app.get("/auth/setup/{token}", response_model=SetupClaimsResponse)
def preview_setup(token: str) -> SetupClaimsResponse:
    try:
        claims = decode_setup_token(token)
    except _jwt.ExpiredSignatureError:
        raise HTTPException(400, "setup link expired — ask an admin to resend") from None
    except _jwt.InvalidTokenError:
        raise HTTPException(400, "invalid setup link") from None
    if user_store.token_was_used(claims["jti"]):
        raise HTTPException(409, "this setup link has already been used")
    if user_store.get_hash(claims["ad_id"]):
        raise HTTPException(409, "password already set for this AD-ID")
    if not user_store.exists(claims["ad_id"]):
        raise HTTPException(404, "AD-ID not recognized")
    user = next(
        (u for u in user_store.list_users() if u.username == claims["ad_id"]),
        None,
    )
    return SetupClaimsResponse(
        ad_id=claims["ad_id"], email=claims["email"], expires_at=claims["exp"],
        phone_number=getattr(user, "phone_number", None) if user else None,
    )


@app.post("/auth/setup/{token}")
def complete_setup(token: str, body: SetupPasswordRequest) -> dict[str, bool]:
    try:
        claims = decode_setup_token(token)
    except _jwt.ExpiredSignatureError:
        raise HTTPException(400, "setup link expired — ask an admin to resend") from None
    except _jwt.InvalidTokenError:
        raise HTTPException(400, "invalid setup link") from None
    if not user_store.mark_token_used(claims["jti"]):
        raise HTTPException(409, "this setup link has already been used")
    if user_store.get_hash(claims["ad_id"]):
        raise HTTPException(409, "password already set for this AD-ID")
    try:
        pw_hash = hash_password(body.password)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    user_store.set_password(claims["ad_id"], pw_hash)
    # Issue the user's long-lived EA API key now so it's ready when
    # they download the copy-trading EA. Idempotent on re-activation.
    try:
        user_store.ensure_ea_api_key(claims["ad_id"])
    except Exception:
        log.exception("ensure_ea_api_key failed for %s", claims["ad_id"])
    return {"activated": True}


# ---------- 2FA enrollment ----------

class TOTPStatusResponse(BaseModel):
    enabled: bool
    pending: bool
    enrolled_at: str | None = None


class TOTPEnrollResponse(BaseModel):
    secret: str
    provisioning_uri: str


class TOTPCodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


@app.get("/auth/2fa/status", response_model=TOTPStatusResponse)
def totp_status(user: dict = Depends(current_user)) -> TOTPStatusResponse:
    s = _totp().status(user["username"])
    return TOTPStatusResponse(enabled=s.enabled, pending=s.pending, enrolled_at=s.enrolled_at)


@app.post("/auth/2fa/enroll", response_model=TOTPEnrollResponse)
def totp_enroll(user: dict = Depends(current_user)) -> TOTPEnrollResponse:
    """Stage a fresh TOTP secret. Caller scans the otpauth URI in their
    authenticator app and POSTs /auth/2fa/confirm with the first code.
    Re-running overwrites the pending value but leaves an active secret
    intact, so a botched enrollment doesn't lock the user out.
    """
    secret = generate_secret()
    _totp().stage_pending(user["username"], secret)
    return TOTPEnrollResponse(
        secret=secret,
        provisioning_uri=provisioning_uri(secret, account=user["username"]),
    )


@app.post("/auth/2fa/confirm", response_model=TOTPStatusResponse)
def totp_confirm(
    body: TOTPCodeRequest,
    user: dict = Depends(current_user),
) -> TOTPStatusResponse:
    """Promote the pending secret to active once a valid code is presented."""
    store = _totp()
    pending = store.get_pending_secret(user["username"])
    if pending is None:
        raise HTTPException(400, "no pending enrollment — call /auth/2fa/enroll first")
    if not verify_code(pending, body.code):
        raise HTTPException(401, "invalid code")
    store.activate(user["username"], pending)
    s = store.status(user["username"])
    return TOTPStatusResponse(enabled=s.enabled, pending=s.pending, enrolled_at=s.enrolled_at)


@app.post("/auth/2fa/disable", response_model=TOTPStatusResponse)
def totp_disable(
    body: TOTPCodeRequest,
    user: dict = Depends(current_user),
) -> TOTPStatusResponse:
    """Turn 2FA off. Requires a current code so a hijacked session token
    alone can't drop the second factor.
    """
    store = _totp()
    secret = store.get_active_secret(user["username"])
    if secret is None:
        raise HTTPException(400, "2FA is not enabled")
    if not verify_code(secret, body.code):
        raise HTTPException(401, "invalid code")
    store.disable(user["username"])
    return TOTPStatusResponse(enabled=False, pending=False, enrolled_at=None)


class CalendarEventResponse(BaseModel):
    event_time: str
    currency: str
    impact: str
    title: str
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    source: str


class BlackoutStatusResponse(BaseModel):
    symbol: str
    blackout: bool
    enabled: bool
    before_min: int
    after_min: int
    current_event: CalendarEventResponse | None = None
    next_event: CalendarEventResponse | None = None
    minutes_until_next: float | None = None


@app.get("/calendar/events", response_model=list[CalendarEventResponse])
def calendar_events(
    hours_ahead: int = 24,
    symbol: str | None = None,
    _user: dict = Depends(current_user),
) -> list[CalendarEventResponse]:
    """Upcoming high/medium/low events in the next `hours_ahead` hours.

    If `symbol` is passed, filter to events whose currency affects that symbol
    (and honor the configured impact filter); otherwise return everything in
    the window regardless of impact, so the dashboard can show a full agenda.
    """
    from datetime import datetime, timedelta, timezone
    from src.econ_calendar.symbols import currencies_for_symbol

    hours_ahead = max(1, min(hours_ahead, 7 * 24))
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours_ahead)
    if symbol:
        ccys = currencies_for_symbol(symbol)
        if not ccys:
            return []
        events = calendar_store.events_in_window(
            currencies=ccys, start=now, end=end, impacts=calendar_policy.impacts
        )
    else:
        events = calendar_store.events_in_window(
            currencies=("USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"),
            start=now, end=end,
            impacts=("high", "medium", "low"),
        )
    return [CalendarEventResponse(**e.to_dict()) for e in events]


@app.get("/calendar/blackout/{symbol}", response_model=BlackoutStatusResponse)
def calendar_blackout(
    symbol: str,
    _user: dict = Depends(current_user),
) -> BlackoutStatusResponse:
    status_dict = calendar_checker.status(symbol)
    return BlackoutStatusResponse(**status_dict)


class RegimeResponse(BaseModel):
    symbol: str
    trend: str
    volatility: str
    label: str
    adx: float | None = None
    plus_di: float | None = None
    minus_di: float | None = None
    atr: float | None = None
    atr_pct: float | None = None
    timestamp: str | None = None
    stored_at: str | None = None


@app.get("/regime/{symbol}", response_model=RegimeResponse)
def regime_for_symbol(
    symbol: str,
    _user: dict = Depends(current_user),
) -> RegimeResponse:
    """Latest regime snapshot for `symbol`.

    Written by the bot on every tick. Returns an 'unknown' shell if the bot
    has never classified this symbol (e.g. the bot isn't running yet).
    """
    data = regime_store.get(symbol) or empty_snapshot_dict(symbol)
    data["symbol"] = symbol
    return RegimeResponse(**{k: v for k, v in data.items() if k in RegimeResponse.model_fields})


class CorrelationPair(BaseModel):
    symbol_a: str
    symbol_b: str
    value: float
    window_bars: int
    computed_at: str


class CorrelationResponse(BaseModel):
    pairs: list[CorrelationPair]
    count: int


@app.get("/correlation", response_model=CorrelationResponse)
def correlation_pairs(_user: dict = Depends(current_user)) -> CorrelationResponse:
    """All known pairwise correlations, sorted by absolute value.

    Bot writes these on a refresh cycle. Empty list means the bot hasn't
    populated the store yet (or is running with a single symbol).
    """
    pairs = correlation_store.all_pairs()
    return CorrelationResponse(
        pairs=[CorrelationPair(**p) for p in pairs],
        count=len(pairs),
    )


class DriftMetric(BaseModel):
    name: str
    baseline: float
    live: float
    delta: float
    delta_pct: float


class DriftBaselinePayload(BaseModel):
    strategy: str
    symbol: str
    trade_count: int
    win_rate: float
    avg_r: float
    avg_trades_per_day: float
    source: str
    computed_at: str


class DriftReportModel(BaseModel):
    strategy: str
    symbol: str
    status: Literal["ok", "warn", "danger", "unknown"]
    live_trade_count: int
    baseline: DriftBaselinePayload | None
    metrics: list[DriftMetric]
    note: str


class DriftResponse(BaseModel):
    reports: list[DriftReportModel]
    count: int
    cached_at: str


# Memoize the drift report for a short window — the dashboard polls every
# few seconds and recomputing means N small SQLite scans per pair. The
# default TTL is short enough that operators see fresh numbers within the
# next refresh cycle.
_drift_cache: dict[str, object] = {"value": None, "expires_at": 0.0}


def _drift_cache_ttl() -> float:
    try:
        return float(os.getenv("DRIFT_CACHE_TTL_S", "60"))
    except ValueError:
        return 60.0


@app.get("/drift", response_model=DriftResponse)
def drift_reports(_user: dict = Depends(current_user)) -> DriftResponse:
    """Live-vs-backtest drift status per known baseline."""
    import time
    now = time.monotonic()
    if _drift_cache["value"] is not None and now < _drift_cache["expires_at"]:
        return _drift_cache["value"]  # type: ignore[return-value]

    reports = drift_monitor.report()
    payload = DriftResponse(
        reports=[DriftReportModel(**r.to_dict()) for r in reports],
        count=len(reports),
        cached_at=datetime.now(timezone.utc).isoformat(),
    )
    _drift_cache["value"] = payload
    _drift_cache["expires_at"] = now + _drift_cache_ttl()
    return payload


class FillSymbolStats(BaseModel):
    symbol: str
    fill_count: int
    rejected_count: int
    avg_slippage_pips: float
    max_slippage_pips: float
    avg_latency_ms: float
    p95_latency_ms: float


class FillStatsResponse(BaseModel):
    symbols: list[FillSymbolStats]
    window_hours: int
    cached_at: str


class FillRow(BaseModel):
    id: int
    trade_id: int | None = None
    symbol: str
    side: str
    event: str
    requested_price: float
    filled_price: float | None = None
    slippage_pips: float | None = None
    latency_ms: float
    broker_ticket: int | None = None
    status: str
    reason: str | None = None
    filled_at: str


class FillsResponse(BaseModel):
    fills: list[FillRow]
    count: int


_fill_stats_cache: dict[str, object] = {"value": None, "expires_at": 0.0, "window": None}


def _fill_stats_ttl() -> float:
    try:
        return float(os.getenv("EXEC_QUALITY_CACHE_TTL_S", "30"))
    except ValueError:
        return 30.0


@app.get("/fills/stats", response_model=FillStatsResponse)
def fill_stats(
    window_hours: int = 24,
    _user: dict = Depends(current_user),
) -> FillStatsResponse:
    """Per-symbol slippage and latency aggregates over the last `window_hours`.

    Cached briefly so dashboard polling (every few seconds) doesn't run the
    GROUP-BY scan repeatedly.
    """
    import time
    now = time.monotonic()
    cached = _fill_stats_cache.get("value")
    if (
        cached is not None
        and now < float(_fill_stats_cache["expires_at"])  # type: ignore[arg-type]
        and _fill_stats_cache.get("window") == window_hours
    ):
        return cached  # type: ignore[return-value]

    rows = fill_store.stats(since_hours=window_hours)
    payload = FillStatsResponse(
        symbols=[
            FillSymbolStats(
                symbol=r.symbol,
                fill_count=r.fill_count,
                rejected_count=r.rejected_count,
                avg_slippage_pips=r.avg_slippage_pips,
                max_slippage_pips=r.max_slippage_pips,
                avg_latency_ms=r.avg_latency_ms,
                p95_latency_ms=r.p95_latency_ms,
            )
            for r in rows
        ],
        window_hours=window_hours,
        cached_at=datetime.now(timezone.utc).isoformat(),
    )
    _fill_stats_cache["value"] = payload
    _fill_stats_cache["expires_at"] = now + _fill_stats_ttl()
    _fill_stats_cache["window"] = window_hours
    return payload


@app.get("/fills", response_model=FillsResponse)
def recent_fills(
    limit: int = 50,
    _user: dict = Depends(current_user),
) -> FillsResponse:
    rows = fill_store.recent(limit=max(1, min(limit, 500)))
    return FillsResponse(
        fills=[FillRow(**{k: r.get(k) for k in FillRow.model_fields}) for r in rows],
        count=len(rows),
    )


# ---------- Allocator ----------

class AllocationModel(BaseModel):
    strategy: str
    symbol: str
    role: Literal["champion", "challenger", "probe", "cold"]
    weight: float
    sample_size: int
    avg_r: float
    win_rate: float
    note: str
    updated_at: str


class AllocatorResponse(BaseModel):
    allocations: list[AllocationModel]
    count: int
    cached_at: str


# Cached because the dashboard polls every few seconds; rereading the same
# small table that often is wasteful when the bot only refreshes weights
# every ~60 ticks anyway.
_allocator_cache: dict[str, object] = {"value": None, "expires_at": 0.0}


def _allocator_cache_ttl() -> float:
    try:
        return float(os.getenv("ALLOCATOR_CACHE_TTL_S", "30"))
    except ValueError:
        return 30.0


@app.get("/allocator", response_model=AllocatorResponse)
def allocator_state(_user: dict = Depends(current_user)) -> AllocatorResponse:
    """Most recent champion-challenger allocations published by the bot."""
    import time
    now = time.monotonic()
    if _allocator_cache["value"] is not None and now < _allocator_cache["expires_at"]:
        return _allocator_cache["value"]  # type: ignore[return-value]

    allocations = allocation_store.all()
    payload = AllocatorResponse(
        allocations=[AllocationModel(**a.to_dict()) for a in allocations],
        count=len(allocations),
        cached_at=datetime.now(timezone.utc).isoformat(),
    )
    _allocator_cache["value"] = payload
    _allocator_cache["expires_at"] = now + _allocator_cache_ttl()
    return payload


# ---------- Explain this trade ----------

class TradeExplanationModel(BaseModel):
    trade_id: int
    strategy: str
    symbol: str
    side: str
    signal_price: float
    signal_stop: float
    signal_target: float
    risk_reward: float
    stop_distance_pips: float
    lot_size: float
    account_balance: float
    opened_at: str
    regime_trend: str | None = None
    regime_volatility: str | None = None
    regime_label: str | None = None
    regime_adx: float | None = None
    regime_atr_pct: float | None = None
    allocator_role: str | None = None
    allocator_weight: float | None = None
    ml_filter_passed: bool | None = None
    notes: str = ""
    indicators: dict = Field(default_factory=dict)
    bars: list = Field(default_factory=list)
    overlays: list = Field(default_factory=list)
    subplots: list = Field(default_factory=list)


@app.get("/trades/{trade_id}/explain", response_model=TradeExplanationModel)
def explain_trade(
    trade_id: int, _user: dict = Depends(current_user)
) -> TradeExplanationModel:
    """Return the captured decision context for one trade.

    Returns 404 for trades that pre-date the explanation feature, or for
    trade IDs that don't exist. The UI surfaces this as 'no explanation
    logged' so operators understand why some trades are blank.
    """
    exp = explanation_store.get(trade_id)
    if exp is None:
        raise HTTPException(404, f"no explanation for trade {trade_id}")
    return TradeExplanationModel(**exp.to_dict())


# ---------- Post-trade narrator ----------

class TradeNarrativeModel(BaseModel):
    trade_id: int
    narrative: str
    provider: str
    model: str | None = None
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    created_at: str


@app.get("/trades/{trade_id}/narrative", response_model=TradeNarrativeModel)
def trade_narrative(
    trade_id: int, _user: dict = Depends(current_user)
) -> TradeNarrativeModel:
    """LLM-written 2-3 sentence post-mortem for one closed trade.

    Returns 404 when narration is off, the trade is still open, or the
    LLM call failed at close-time (errors don't crash the bot, so a stale
    trade simply has no row). The dashboard surfaces this as 'no
    narrative yet'.
    """
    n = narrative_store.get(trade_id)
    if n is None:
        raise HTTPException(404, f"no narrative for trade {trade_id}")
    return TradeNarrativeModel(**n.to_dict())


# ---------- Replay-with-different-params ----------

class ReplayRequestBody(BaseModel):
    stop_loss: float | None = None
    take_profit: float | None = None
    sl_mult: float | None = None
    tp_mult: float | None = None


class ReplayResponse(BaseModel):
    trade_id: int
    side: str
    symbol: str
    entry_price: float
    original_stop: float
    original_target: float
    original_pnl: float
    original_close_reason: str | None = None
    replay_stop: float
    replay_target: float
    replay_exit_price: float | None = None
    replay_close_reason: str
    replay_pnl: float
    replay_r_multiple: float | None = None
    pnl_delta: float
    bars_walked: int


@app.post("/trades/{trade_id}/replay", response_model=ReplayResponse)
def replay_trade(
    trade_id: int,
    body: ReplayRequestBody,
    _user: dict = Depends(current_user),
) -> ReplayResponse:
    """Re-walk a closed trade with tweaked SL/TP and report the alternative
    outcome. Either pass absolute prices (`stop_loss`, `take_profit`) or
    multipliers (`sl_mult`, `tp_mult`) applied to the original distance
    from entry. With an empty body the replay returns the original outcome
    unchanged — useful for sanity-checking the engine.

    Returns 404 only when the trade itself doesn't exist or hasn't closed.
    A trade with no recorded path still returns 200 with `replay_close_reason
    = 'no_path'` so the UI can explain why no alternative was computed.
    """
    engine = ReplayEngine(path_store, db_path=_DB)
    result = engine.replay(
        trade_id,
        ReplayRequest(
            stop_loss=body.stop_loss,
            take_profit=body.take_profit,
            sl_mult=body.sl_mult,
            tp_mult=body.tp_mult,
        ),
    )
    if result is None:
        raise HTTPException(404, f"trade {trade_id} not found or still open")
    return ReplayResponse(**result.to_dict())


# ---------- Watchdog ----------

class HeartbeatModel(BaseModel):
    process_name: str
    last_tick_at: str
    age_seconds: float
    tick_count: int
    pid: int | None = None
    last_error: str | None = None


class WatchdogActionModel(BaseModel):
    taken_at: str
    action: str
    reason: str
    success: bool
    detail: str | None = None


class WatchdogResponse(BaseModel):
    heartbeats: list[HeartbeatModel]
    recent_actions: list[WatchdogActionModel]


@app.get("/watchdog", response_model=WatchdogResponse)
def watchdog_status(_user: dict = Depends(current_user)) -> WatchdogResponse:
    """Surface heartbeat freshness and recent watchdog actions to the dashboard."""
    from src.watchdog import Watchdog, WatchdogConfig
    now = datetime.now(timezone.utc)
    heartbeats = [
        HeartbeatModel(
            process_name=hb.process_name,
            last_tick_at=hb.last_tick_at.isoformat(),
            age_seconds=hb.age_seconds(now),
            tick_count=hb.tick_count,
            pid=hb.pid,
            last_error=hb.last_error,
        )
        for hb in heartbeat_store.all()
    ]
    # Read recent actions directly — we don't need the decision logic here.
    wd = Watchdog(
        db_path=_DB,
        heartbeat_store=heartbeat_store,
        broker_status_store=broker_status_store,
        restart_bot_cb=lambda: (False, "API process can't restart services"),
        recycle_mt5_cb=lambda: (False, "API process can't recycle MT5"),
        config=WatchdogConfig.from_env(),
    )
    actions = [
        WatchdogActionModel(
            taken_at=(a.taken_at.isoformat() if a.taken_at else ""),
            action=a.action.value,
            reason=a.reason,
            success=a.success,
            detail=a.detail,
        )
        for a in wd.recent_actions(limit=20)
    ]
    return WatchdogResponse(heartbeats=heartbeats, recent_actions=actions)


# ---------- PropFirm ----------

class PropFirmResponse(BaseModel):
    enabled: bool
    initialized: bool
    preset: str | None = None
    initial_balance: float | None = None
    current_equity: float | None = None
    peak_equity: float | None = None
    profit_amount: float | None = None
    profit_target_amount: float | None = None
    profit_target_pct: float | None = None
    profit_remaining_amount: float | None = None
    daily_start_equity: float | None = None
    daily_loss_amount: float | None = None
    daily_loss_limit_amount: float | None = None
    daily_loss_pct: float | None = None
    max_daily_loss_pct: float | None = None
    total_drawdown_amount: float | None = None
    total_drawdown_limit_amount: float | None = None
    total_drawdown_pct: float | None = None
    max_total_drawdown_pct: float | None = None
    drawdown_from_peak: bool | None = None
    trading_days_count: int | None = None
    min_trading_days: int | None = None
    killed_today: bool | None = None
    killed_permanently: bool | None = None
    killed_reason: str | None = None
    max_lot_size: float | None = None
    require_stop_loss: bool | None = None
    updated_at: str | None = None


@app.get("/propfirm", response_model=PropFirmResponse)
def propfirm_status(_user: dict = Depends(current_user)) -> PropFirmResponse:
    """Challenge progress for the dashboard. Off → enabled=false, no other fields."""
    enabled = os.getenv("PROPFIRM_ENABLED", "0").strip() not in ("0", "false", "False", "")
    if not enabled:
        return PropFirmResponse(enabled=False, initialized=False)
    today = journal.summary_today()
    equity = state.balance + today["pnl"]
    guard = PropFirmGuard(policy_from_env(), propfirm_store)
    snap = guard.progress(equity)
    return PropFirmResponse(enabled=True, **snap)


# ---------- Voice kill switch ----------

class VoiceCommandRequest(BaseModel):
    transcript: str = Field(min_length=1, max_length=500)


class VoiceCommandResponse(BaseModel):
    matched: bool
    phrase: str | None = None
    score: float
    active: bool
    message: str


class VoiceLogModel(BaseModel):
    id: int
    received_at: str
    username: str
    transcript: str
    matched: bool
    phrase: str | None = None
    score: float


class VoiceStatusResponse(BaseModel):
    enabled: bool
    active: bool
    triggered_at: str | None = None
    triggered_by: str | None = None
    phrase: str | None = None
    cleared_at: str | None = None
    cleared_by: str | None = None


def _voice_enabled() -> bool:
    return os.getenv("VOICE_KILLSWITCH_ENABLED", "0").strip() not in ("0", "false", "False", "")


@app.get("/voice/status", response_model=VoiceStatusResponse)
def voice_status(_user: dict = Depends(current_user)) -> VoiceStatusResponse:
    s = voice_kill_flag.state()
    return VoiceStatusResponse(
        enabled=_voice_enabled(),
        active=s.active,
        triggered_at=s.triggered_at,
        triggered_by=s.triggered_by,
        phrase=s.phrase,
        cleared_at=s.cleared_at,
        cleared_by=s.cleared_by,
    )


@app.post("/voice/command", response_model=VoiceCommandResponse)
def voice_command(
    body: VoiceCommandRequest,
    user: dict = Depends(current_user),
) -> VoiceCommandResponse:
    """Receive a transcribed phrase from a client (mobile STT) and decide
    whether it's a kill command. Every attempt — match or miss — is logged
    for audit. Note: this endpoint is intentionally NOT behind require_2fa,
    because the whole point of a voice kill is to halt the bot fast in an
    emergency, possibly without unlocking the phone. Re-arming via
    /voice/clear IS gated by 2FA — coming back online should be the slow
    path, not the fast one.
    """
    if not _voice_enabled():
        raise HTTPException(503, "voice killswitch disabled (set VOICE_KILLSWITCH_ENABLED=1)")
    config = VoiceKillConfig.from_env()
    result = match_phrase(body.transcript, config)
    voice_log_store.record(username=user["username"], transcript=body.transcript, result=result)
    if result.matched and result.phrase is not None:
        voice_kill_flag.activate(username=user["username"], phrase=result.phrase)
        return VoiceCommandResponse(
            matched=True,
            phrase=result.phrase,
            score=result.score,
            active=True,
            message=f"kill switch tripped on phrase '{result.phrase}'",
        )
    return VoiceCommandResponse(
        matched=False,
        phrase=None,
        score=result.score,
        active=voice_kill_flag.is_active(),
        message="no kill phrase matched",
    )


@app.get("/voice/log", response_model=list[VoiceLogModel])
def voice_log(
    limit: int = 50,
    _user: dict = Depends(current_user),
) -> list[VoiceLogModel]:
    return [
        VoiceLogModel(
            id=e.id, received_at=e.received_at, username=e.username,
            transcript=e.transcript, matched=e.matched,
            phrase=e.phrase, score=e.score,
        )
        for e in voice_log_store.recent(limit=limit)
    ]


@app.post("/voice/clear", response_model=VoiceStatusResponse)
def voice_clear(user: dict = Depends(require_2fa)) -> VoiceStatusResponse:
    """Re-arm the bot after a kill. Gated by 2FA so you can't accidentally
    reverse a kill the way you triggered it (a stray voice command).
    """
    voice_kill_flag.clear(username=user["username"])
    s = voice_kill_flag.state()
    return VoiceStatusResponse(
        enabled=_voice_enabled(),
        active=s.active,
        triggered_at=s.triggered_at,
        triggered_by=s.triggered_by,
        phrase=s.phrase,
        cleared_at=s.cleared_at,
        cleared_by=s.cleared_by,
    )


_MT5_DIR = Path(__file__).parent.parent.parent / "mt5"


@app.get("/download/ea", include_in_schema=False)
def download_ea() -> Response:
    """Serve AntiGreedCopier bundled as a zip — the .mq5 itself plus the
    BMP assets the on-chart panel uses. Single click in the dashboard,
    user unzips into MQL5\\ where the structure matches (Experts/ for the
    .mq5, Files/ for the BMPs).
    """
    import io
    import zipfile

    mq5 = (_MT5_DIR / "AntiGreedCopier.mq5").resolve()
    if not mq5.is_file():
        raise HTTPException(404, "AntiGreedCopier.mq5 not found on server")
    bmps = [p for p in [
        _MT5_DIR / "antigreed-logo.bmp",
        _MT5_DIR / "antigreed-buy.bmp",
        _MT5_DIR / "antigreed-sell.bmp",
    ] if p.is_file()]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(mq5, arcname="Experts/AntiGreedCopier.mq5")
        for bmp in bmps:
            zf.write(bmp, arcname=f"Files/{bmp.name}")
        zf.writestr(
            "README.txt",
            "AntiGreed Copier — install\n"
            "==========================\n"
            "1. In MetaTrader 5: File → Open Data Folder\n"
            "2. Open the MQL5 folder inside.\n"
            "3. Copy this zip's Experts/ contents into MQL5/Experts/.\n"
            "4. Copy this zip's Files/ contents into MQL5/Files/.\n"
            "5. In MetaEditor (F4): open AntiGreedCopier.mq5, press F7.\n"
            "6. In MT5: Tools → Options → Expert Advisors → whitelist your\n"
            "   API URL, then drag the EA onto any chart.\n"
        )
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Cache-Control": "no-store, must-revalidate",
            "Content-Disposition": 'attachment; filename="AntiGreedCopier.zip"',
        },
    )


@app.get("/download/ea/mq5", include_in_schema=False)
def download_ea_mq5() -> FileResponse:
    """Raw .mq5 — for users who already have the BMP assets and just
    want the latest source to recompile.
    """
    candidate = (_MT5_DIR / "AntiGreedCopier.mq5").resolve()
    if not candidate.is_file():
        raise HTTPException(404, "AntiGreedCopier.mq5 not found on server")
    return FileResponse(
        candidate,
        media_type="application/octet-stream",
        filename="AntiGreedCopier.mq5",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    # Serve static files ourselves so we can attach no-store headers. Using
    # StaticFiles caches aggressively by default, which means edits to app.js
    # don't reach the browser until a hard reload.
    @app.get("/static/{path:path}", include_in_schema=False)
    def static_file(path: str) -> FileResponse:
        # Keep within the static dir — reject any path that escapes it.
        candidate = (_STATIC_DIR / path).resolve()
        if not str(candidate).startswith(str(_STATIC_DIR.resolve())) or not candidate.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(candidate, headers={"Cache-Control": "no-store, must-revalidate"})

    @app.get("/", include_in_schema=False)
    def root() -> FileResponse:
        # no-store on the SPA shell so the browser always fetches the latest
        # Alpine wiring — otherwise a stale cached index.html hides new UI.
        return FileResponse(
            _STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    @app.get("/setup", include_in_schema=False)
    def setup_page() -> FileResponse:
        return FileResponse(
            _STATIC_DIR / "setup.html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )
