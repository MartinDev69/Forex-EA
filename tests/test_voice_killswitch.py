"""Tests for the voice kill switch: phrase matcher, kill flag + audit log,
API endpoints, and the bot-loop integration that halts a tick when the
flag is active.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.voice.killswitch import (
    KillSwitchFlag,
    VoiceKillConfig,
    VoiceLogStore,
    match_phrase,
    normalize,
)

os.environ.setdefault("AUTH_SECRET", "test-secret-at-least-32-characters-long-xxxx")
_SECRET = os.environ["AUTH_SECRET"]


# ---------------------------------------------------------------- matching

def test_normalize_handles_punctuation_case_whitespace():
    assert normalize("Stop, Trading!") == "stop trading"
    assert normalize("  STOP   trading\n\t") == "stop trading"
    assert normalize("") == ""
    assert normalize("???!!!") == ""


def test_match_phrase_substring_hits_with_full_score():
    cfg = VoiceKillConfig(phrases=("stop trading",))
    r = match_phrase("Please stop trading right now.", cfg)
    assert r.matched is True
    assert r.phrase == "stop trading"
    assert r.score == 1.0


def test_match_phrase_case_and_punctuation_insensitive():
    cfg = VoiceKillConfig(phrases=("Stop Trading",))
    r = match_phrase("STOP, TRADING.", cfg)
    assert r.matched is True


def test_match_phrase_fuzzy_handles_stt_noise():
    """STT often returns near-misses ('stop trade in' for 'stop trading').
    Threshold should accept the obvious near-misses without going wild.
    """
    cfg = VoiceKillConfig(phrases=("stop trading",), fuzzy_threshold=0.80)
    r = match_phrase("stop trade in", cfg)
    assert r.matched is True
    assert r.score >= 0.80


def test_match_phrase_rejects_unrelated_speech():
    cfg = VoiceKillConfig(phrases=("stop trading",), fuzzy_threshold=0.80)
    r = match_phrase("the weather is nice today", cfg)
    assert r.matched is False
    assert r.phrase is None


def test_match_phrase_picks_best_scoring_phrase():
    cfg = VoiceKillConfig(phrases=("emergency stop", "halt the bot"))
    r = match_phrase("emergency stop", cfg)
    assert r.matched is True
    assert r.phrase == "emergency stop"


def test_match_phrase_empty_input():
    cfg = VoiceKillConfig()
    r = match_phrase("", cfg)
    assert r.matched is False
    assert r.score == 0.0


def test_config_from_env_parses_pipe_list():
    cfg = VoiceKillConfig.from_env({
        "VOICE_KILL_PHRASES": "halt now | red button | mayday mayday",
        "VOICE_FUZZY_THRESHOLD": "0.9",
    })
    assert cfg.phrases == ("halt now", "red button", "mayday mayday")
    assert cfg.fuzzy_threshold == 0.9


def test_config_from_env_clamps_threshold():
    assert VoiceKillConfig.from_env({"VOICE_FUZZY_THRESHOLD": "0.1"}).fuzzy_threshold == 0.5
    assert VoiceKillConfig.from_env({"VOICE_FUZZY_THRESHOLD": "5"}).fuzzy_threshold == 1.0
    # Garbage falls back to default.
    assert VoiceKillConfig.from_env({"VOICE_FUZZY_THRESHOLD": "abc"}).fuzzy_threshold == 0.80


def test_config_from_env_uses_defaults_when_unset():
    cfg = VoiceKillConfig.from_env({})
    assert "stop trading" in cfg.phrases
    assert cfg.fuzzy_threshold == 0.80


# ---------------------------------------------------------------- KillSwitchFlag

def test_flag_starts_inactive(tmp_path: Path):
    flag = KillSwitchFlag(tmp_path / "trades.db")
    assert flag.is_active() is False
    s = flag.state()
    assert s.active is False
    assert s.triggered_at is None


def test_flag_activate_then_clear(tmp_path: Path):
    flag = KillSwitchFlag(tmp_path / "trades.db")
    flag.activate(username="alice", phrase="stop trading")
    assert flag.is_active() is True
    s = flag.state()
    assert s.triggered_by == "alice"
    assert s.phrase == "stop trading"
    assert s.triggered_at is not None
    assert s.cleared_at is None

    cleared = flag.clear(username="bob")
    assert cleared is True
    assert flag.is_active() is False
    s = flag.state()
    assert s.cleared_by == "bob"
    assert s.cleared_at is not None
    # triggered_at + by are preserved so the audit trail survives a clear.
    assert s.triggered_by == "alice"


def test_flag_clear_idempotent(tmp_path: Path):
    flag = KillSwitchFlag(tmp_path / "trades.db")
    assert flag.clear(username="alice") is False
    flag.activate(username="alice", phrase="halt the bot")
    assert flag.clear(username="alice") is True
    assert flag.clear(username="alice") is False


def test_flag_persists_across_instances(tmp_path: Path):
    """Cross-process semantics: API process trips, bot process reads."""
    db = tmp_path / "trades.db"
    KillSwitchFlag(db).activate(username="api", phrase="stop trading")
    # New instance, same DB — simulates the bot process opening its own connection.
    assert KillSwitchFlag(db).is_active() is True


# ---------------------------------------------------------------- VoiceLogStore

def test_log_store_records_match_and_miss(tmp_path: Path):
    from src.voice.killswitch import MatchResult

    store = VoiceLogStore(tmp_path / "trades.db")
    store.record(
        username="alice",
        transcript="stop trading now",
        result=MatchResult(True, "stop trading", 1.0, "stop trading now"),
    )
    store.record(
        username="alice",
        transcript="what's the weather",
        result=MatchResult(False, None, 0.2, "whats the weather"),
    )
    rows = store.recent()
    assert len(rows) == 2
    # Newest first.
    assert rows[0].matched is False
    assert rows[1].matched is True
    assert rows[1].phrase == "stop trading"


def test_log_store_recent_respects_limit(tmp_path: Path):
    from src.voice.killswitch import MatchResult

    store = VoiceLogStore(tmp_path / "trades.db")
    for i in range(10):
        store.record(
            username="alice",
            transcript=f"attempt {i}",
            result=MatchResult(False, None, 0.0, f"attempt {i}"),
        )
    assert len(store.recent(limit=3)) == 3


# ---------------------------------------------------------------- API

@pytest.fixture
def fresh_server(tmp_path: Path, monkeypatch):
    from src.api import auth as auth_module
    from src.api import server as server_module
    from src.api.totp_store import TOTPStore
    from src.api.users import UserStore
    from src.execution.journal import TradeJournal
    from src.execution.strategy_toggles import StrategyToggleStore

    db = tmp_path / "trades.db"
    monkeypatch.setattr(server_module, "_DB", db)
    monkeypatch.setattr(server_module, "journal", TradeJournal(db))
    monkeypatch.setattr(server_module, "toggle_store", StrategyToggleStore(db))
    monkeypatch.setattr(server_module, "user_store", UserStore(db))
    monkeypatch.setattr(server_module, "voice_kill_flag", KillSwitchFlag(db))
    monkeypatch.setattr(server_module, "voice_log_store", VoiceLogStore(db))
    monkeypatch.setattr(server_module, "_totp_store", TOTPStore(db, secret=_SECRET))
    monkeypatch.setattr(auth_module, "_rate_limiter", auth_module.LoginRateLimiter())
    monkeypatch.setenv("VOICE_KILLSWITCH_ENABLED", "1")

    stub = lambda: {"username": "alice", "role": "admin"}
    server_module.app.dependency_overrides[auth_module.current_user] = stub
    server_module.app.dependency_overrides[auth_module.require_admin] = stub
    yield server_module
    server_module.app.dependency_overrides.clear()


def test_voice_command_disabled_returns_503(tmp_path: Path, monkeypatch):
    from src.api import auth as auth_module
    from src.api import server as server_module

    db = tmp_path / "trades.db"
    monkeypatch.setattr(server_module, "voice_kill_flag", KillSwitchFlag(db))
    monkeypatch.setattr(server_module, "voice_log_store", VoiceLogStore(db))
    monkeypatch.delenv("VOICE_KILLSWITCH_ENABLED", raising=False)
    server_module.app.dependency_overrides[auth_module.current_user] = (
        lambda: {"username": "alice", "role": "admin"}
    )
    try:
        client = TestClient(server_module.app)
        r = client.post("/voice/command", json={"transcript": "stop trading"})
        assert r.status_code == 503
    finally:
        server_module.app.dependency_overrides.clear()


def test_voice_command_matches_and_trips_flag(fresh_server):
    client = TestClient(fresh_server.app)
    r = client.post("/voice/command", json={"transcript": "please stop trading"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] is True
    assert body["active"] is True
    assert body["phrase"]
    assert fresh_server.voice_kill_flag.is_active() is True


def test_voice_command_miss_does_not_trip(fresh_server):
    client = TestClient(fresh_server.app)
    r = client.post("/voice/command", json={"transcript": "what's the weather like"})
    assert r.status_code == 200
    body = r.json()
    assert body["matched"] is False
    assert body["active"] is False
    assert fresh_server.voice_kill_flag.is_active() is False


def test_voice_command_logs_every_attempt(fresh_server):
    client = TestClient(fresh_server.app)
    client.post("/voice/command", json={"transcript": "stop trading"})
    client.post("/voice/command", json={"transcript": "weather report"})
    r = client.get("/voice/log")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    transcripts = {row["transcript"] for row in rows}
    assert transcripts == {"stop trading", "weather report"}


def test_voice_status_reflects_flag(fresh_server):
    client = TestClient(fresh_server.app)
    s = client.get("/voice/status").json()
    assert s["enabled"] is True
    assert s["active"] is False

    client.post("/voice/command", json={"transcript": "stop trading"})
    s = client.get("/voice/status").json()
    assert s["active"] is True
    assert s["triggered_by"] == "alice"
    assert s["phrase"]


def test_voice_clear_requires_2fa_when_enrolled(fresh_server):
    """Tripping is the fast path; re-arming after a kill is gated by 2FA."""
    from src.api.totp import generate_code

    client = TestClient(fresh_server.app)
    # Trip the flag.
    client.post("/voice/command", json={"transcript": "stop trading"})
    assert fresh_server.voice_kill_flag.is_active() is True

    # Enroll alice in 2FA.
    secret = client.post("/auth/2fa/enroll").json()["secret"]
    client.post("/auth/2fa/confirm", json={"code": generate_code(secret)})

    # Without code: rejected.
    r = client.post("/voice/clear")
    assert r.status_code == 401
    assert fresh_server.voice_kill_flag.is_active() is True

    # With valid code: cleared.
    r = client.post("/voice/clear", headers={"X-2FA-Code": generate_code(secret)})
    assert r.status_code == 200
    assert r.json()["active"] is False
    assert fresh_server.voice_kill_flag.is_active() is False


def test_voice_clear_open_when_2fa_off(fresh_server):
    """Without 2FA enrolled, /voice/clear works directly (require_2fa is opt-in)."""
    client = TestClient(fresh_server.app)
    client.post("/voice/command", json={"transcript": "stop trading"})
    r = client.post("/voice/clear")
    assert r.status_code == 200
    assert fresh_server.voice_kill_flag.is_active() is False


# ---------------------------------------------------------------- Bot integration

class _AlwaysBuyStrategy:
    name = "always_buy"
    symbol = "EURUSD"
    preferred_regimes: tuple = ()

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    def generate_signal(self, ohlc):
        from src.strategies.base import Signal, SignalType
        last = ohlc.iloc[-1]
        price = float(last["close"])
        return Signal(
            type=SignalType.BUY, symbol=self.symbol, timestamp=ohlc.index[-1],
            price=price, stop_loss=price - 0.005, take_profit=price + 0.01, reason="test",
        )


class _FixedFeed:
    def __init__(self, ohlc: pd.DataFrame) -> None:
        self._ohlc = ohlc

    def latest_bars(self, symbol, timeframe, count):
        return self._ohlc.tail(count).copy()


def _sample_ohlc(bars: int = 100, base: float = 1.10) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    close = base + np.cumsum(rng.normal(0, 0.0005, bars))
    high = close + 0.0008
    low = close - 0.0008
    idx = pd.date_range("2024-01-01", periods=bars, freq="15min")
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": 100},
        index=idx,
    )


def test_bot_tick_halts_when_kill_flag_active(tmp_path: Path):
    """Flag set → tick returns 0, bot.state.running=False, no orders placed."""
    from src.bot import Bot, BotConfig
    from src.execution.journal import TradeJournal
    from src.execution.mock import MockExecutor
    from src.risk.risk_manager import RiskLimits, RiskManager

    db = tmp_path / "trades.db"
    flag = KillSwitchFlag(db)
    flag.activate(username="alice", phrase="stop trading")

    feed = _FixedFeed(_sample_ohlc())
    executor = MockExecutor(starting_balance=10_000)
    risk = RiskManager(RiskLimits(risk_per_trade=0.01, max_open_trades=5))
    journal = TradeJournal(db)

    bot = Bot(
        config=BotConfig(symbols=["EURUSD"], timeframe="M15", poll_interval_s=1),
        strategies={"EURUSD": [_AlwaysBuyStrategy("EURUSD")]},
        data_feed=feed,
        executor=executor,
        risk_manager=risk,
        journal=journal,
        kill_switch_flag=flag,
    )
    bot.state.running = True

    acted = bot.tick()
    assert acted == 0
    assert bot.state.running is False
    assert executor.open_orders() == []


def test_bot_tick_runs_normally_when_flag_inactive(tmp_path: Path):
    """Sanity check: passing a flag that's NOT tripped doesn't break the loop."""
    from src.bot import Bot, BotConfig
    from src.execution.journal import TradeJournal
    from src.execution.mock import MockExecutor
    from src.risk.risk_manager import RiskLimits, RiskManager

    db = tmp_path / "trades.db"
    flag = KillSwitchFlag(db)  # inactive

    feed = _FixedFeed(_sample_ohlc())
    executor = MockExecutor(starting_balance=10_000)
    risk = RiskManager(RiskLimits(risk_per_trade=0.01, max_open_trades=5))
    journal = TradeJournal(db)

    bot = Bot(
        config=BotConfig(symbols=["EURUSD"], timeframe="M15", poll_interval_s=1),
        strategies={"EURUSD": [_AlwaysBuyStrategy("EURUSD")]},
        data_feed=feed,
        executor=executor,
        risk_manager=risk,
        journal=journal,
        kill_switch_flag=flag,
    )
    bot.state.running = True

    acted = bot.tick()
    assert acted == 1
    assert bot.state.running is True
    assert len(executor.open_orders()) == 1
