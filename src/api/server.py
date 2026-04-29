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

import os
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

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.api import brokers as broker_presets
from src.api.auth import (
    LoginRateLimiter,
    _secret,
    authenticate,
    client_ip,
    create_token,
    current_user,
    hash_password,
    rate_limiter,
    require_admin,
)
from src.api.ad_id import ADMIN_AD_ID, is_user_ad_id
from src.api.broker_config import BrokerConfig, BrokerConfigStore
from src.api.broker_status import BrokerStatusStore
from src.api.mailer import send_setup_email
from src.api.setup_tokens import SETUP_TTL_S, create_setup_token, decode_setup_token
from src.api.totp import generate_secret, provisioning_uri, verify_code
from src.api.totp_store import TOTPStore
from src.api.users import LastAdminError, UserStore
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

@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _calendar_refresher
    interval = int(os.environ.get("CALENDAR_REFRESH_INTERVAL_S", "1800"))
    _calendar_refresher = CalendarRefresher(
        ForexFactoryProvider(), calendar_store, interval_s=interval,
    )
    _calendar_refresher.start()
    try:
        yield
    finally:
        if _calendar_refresher is not None:
            await _calendar_refresher.stop()
            _calendar_refresher = None


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
broker_status_store = BrokerStatusStore(_DB)
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


class AssignUserRequest(BaseModel):
    ad_id: str = Field(min_length=1, max_length=32)
    email: str = Field(min_length=3, max_length=254)


class AssignUserResponse(BaseModel):
    ad_id: str
    email: str
    setup_expires_at: int
    setup_url: str | None = None  # populated in dev mode so admin can copy the link


class SetupPasswordRequest(BaseModel):
    password: str = Field(min_length=12, max_length=72)


class SetupClaimsResponse(BaseModel):
    ad_id: str
    email: str
    expires_at: int


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


def _open_positions() -> int:
    rows = journal.recent(limit=200)
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
    return StatusResponse(
        running=state.running,
        mt5_connected=bool(bs.connected) if bs else False,
        last_heartbeat=state.last_heartbeat,
        open_positions=_open_positions(),
    )


@app.get("/account", response_model=AccountResponse)
def account(_user: dict = Depends(current_user)) -> AccountResponse:
    today = journal.summary_today()
    status = broker_status_store.read()
    info = status.account_info if status and status.connected else None
    balance = info["balance"] if info and "balance" in info else state.balance
    equity = info["equity"] if info and "equity" in info else balance + today["pnl"]
    return AccountResponse(
        balance=balance,
        equity=equity,
        open_positions=_open_positions(),
        daily_pnl=today["pnl"],
    )


@app.get("/strategies", response_model=list[StrategyResponse])
def list_strategies(_user: dict = Depends(current_user)) -> list[StrategyResponse]:
    return [StrategyResponse(name=n, enabled=e) for n, e in toggle_store.list().items()]


@app.post("/strategies/{name}/toggle", response_model=StrategyResponse)
def toggle_strategy(name: str, _user: dict = Depends(require_2fa)) -> StrategyResponse:
    try:
        enabled = toggle_store.toggle(name)
    except KeyError:
        raise HTTPException(404, f"strategy '{name}' not found") from None
    return StrategyResponse(name=name, enabled=enabled)


@app.get("/trades", response_model=list[TradeResponse])
def trades(limit: int = 20, _user: dict = Depends(current_user)) -> list[TradeResponse]:
    rows = journal.recent(limit=limit)
    out: list[TradeResponse] = []
    for r in rows:
        out.append(TradeResponse(
            id=r["id"],
            symbol=r["symbol"],
            side=r["side"],
            entry_price=r["entry_price"],
            exit_price=r["exit_price"],
            pnl=r["pnl"] or 0.0,
            opened_at=datetime.fromisoformat(r["opened_at"]),
            closed_at=datetime.fromisoformat(r["closed_at"]) if r["closed_at"] else None,
        ))
    return out


@app.post("/bot/start")
def start_bot(_user: dict = Depends(require_2fa)) -> dict[str, str]:
    state.running = True
    state.last_heartbeat = datetime.utcnow()
    return {"status": "started"}


@app.post("/bot/stop")
def stop_bot(_user: dict = Depends(require_2fa)) -> dict[str, str]:
    state.running = False
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


@app.get("/broker/status", response_model=BrokerStatusResponse)
def get_broker_status(_user: dict = Depends(current_user)) -> BrokerStatusResponse:
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
    return UserResponse(
        username=u.username, role=u.role, email=u.email,
        created_at=u.created_at, password_set=u.password_set,
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
        user_store.assign(body.ad_id, body.email)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return _issue_setup_link(body.ad_id, body.email)


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


def _issue_setup_link(ad_id: str, email: str) -> AssignUserResponse:
    """Mint a fresh setup JWT, try to email it, surface the URL in dev mode."""
    from src.api.mailer import mailer_configured

    token, exp, url = create_setup_token(ad_id, email)
    hours = SETUP_TTL_S // 3600
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
    return SetupClaimsResponse(
        ad_id=claims["ad_id"], email=claims["email"], expires_at=claims["exp"],
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
