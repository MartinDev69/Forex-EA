"""Tests for the post-trade LLM narrator: provider selection, store
roundtrip, prompt composition, end-to-end narrate() against the journal,
and the /trades/{id}/narrative endpoint.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.narrator import (
    NarrativeStore,
    NarratorComposer,
    StubProvider,
    TradeNarrative,
    build_provider,
)
from src.narrator.composer import TradeContext
from src.narrator.provider import LLMResponse


def _now() -> datetime:
    return datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- provider


def test_build_provider_defaults_to_stub():
    p = build_provider({})
    assert p.name == "stub"


def test_build_provider_anthropic_falls_back_without_key():
    p = build_provider({"NARRATOR_PROVIDER": "anthropic"})
    assert p.name == "stub"


def test_build_provider_openai_falls_back_without_key():
    p = build_provider({"NARRATOR_PROVIDER": "openai"})
    assert p.name == "stub"


def test_build_provider_anthropic_with_key():
    p = build_provider({
        "NARRATOR_PROVIDER": "anthropic",
        "NARRATOR_API_KEY": "sk-test",
        "NARRATOR_MODEL": "claude-haiku-4-5-20251001",
    })
    assert p.name == "anthropic"
    assert p.model == "claude-haiku-4-5-20251001"


def test_build_provider_anthropic_uses_env_fallback_key():
    p = build_provider({
        "NARRATOR_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-fallback",
    })
    assert p.name == "anthropic"


def test_build_provider_openai_with_key():
    p = build_provider({
        "NARRATOR_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-test",
    })
    assert p.name == "openai"


def test_stub_provider_echoes_first_lines():
    p = StubProvider()
    resp = p.complete("system", "alpha\nbeta\ngamma\n", max_tokens=100)
    assert "alpha" in resp.text
    assert resp.model == "stub"


# ---------------------------------------------------------------- store


def test_store_roundtrip(tmp_path):
    s = NarrativeStore(tmp_path / "trades.db")
    n = TradeNarrative(
        trade_id=42,
        narrative="bot bought EURUSD, hit target, +1.5R",
        provider="stub",
        model="stub",
        prompt_tokens=10,
        output_tokens=12,
        created_at=_now().isoformat(),
    )
    s.write(n)
    got = s.get(42)
    assert got is not None
    assert got.narrative == n.narrative
    assert got.provider == "stub"
    assert got.prompt_tokens == 10


def test_store_get_missing(tmp_path):
    s = NarrativeStore(tmp_path / "trades.db")
    assert s.get(999) is None


def test_store_overwrite(tmp_path):
    s = NarrativeStore(tmp_path / "trades.db")
    n1 = TradeNarrative(trade_id=1, narrative="v1", provider="stub", created_at=_now().isoformat())
    n2 = TradeNarrative(trade_id=1, narrative="v2", provider="stub", created_at=_now().isoformat())
    s.write(n1)
    s.write(n2)
    assert s.get(1).narrative == "v2"


# ---------------------------------------------------------------- composer prompt


def _ctx(**overrides) -> TradeContext:
    base = dict(
        trade_id=7,
        symbol="EURUSD",
        side="BUY",
        strategy="MACrossover",
        lot_size=0.20,
        entry_price=1.10000,
        exit_price=1.10500,
        stop_loss=1.09800,
        take_profit=1.10500,
        pnl=100.0,
        close_reason="target",
        opened_at="2026-04-26T10:00:00+00:00",
        closed_at="2026-04-26T11:30:00+00:00",
        risk_reward=2.5,
        stop_distance_pips=20.0,
        regime_label="trend_up/normal",
        allocator_role="champion",
        allocator_weight=1.0,
        ml_filter_passed=True,
        avg_slippage_pips=0.3,
        avg_latency_ms=42.0,
    )
    base.update(overrides)
    return TradeContext(**base)


def test_build_prompt_includes_headline_numbers(tmp_path):
    c = NarratorComposer(StubProvider(), NarrativeStore(tmp_path / "trades.db"))
    prompt = c.build_prompt(_ctx())
    assert "EURUSD" in prompt
    assert "+100.00" in prompt
    assert "target" in prompt
    assert "trend_up/normal" in prompt
    assert "champion" in prompt


def test_build_prompt_skips_optional_lines(tmp_path):
    c = NarratorComposer(StubProvider(), NarrativeStore(tmp_path / "trades.db"))
    minimal = _ctx(
        risk_reward=None, stop_distance_pips=None,
        regime_label=None, allocator_role=None, allocator_weight=None,
        ml_filter_passed=None, avg_slippage_pips=None, avg_latency_ms=None,
        close_reason=None,
    )
    prompt = c.build_prompt(minimal)
    assert "Regime" not in prompt
    assert "Allocator" not in prompt
    assert "ML filter" not in prompt
    assert "EURUSD" in prompt


def test_r_multiple_buy_winner():
    c = NarratorComposer(StubProvider(), NarrativeStore(":memory:"))
    r = c._r_multiple(_ctx())  # entry 1.10, stop 1.098, exit 1.105 → 5/2 = 2.5R
    assert r == pytest.approx(2.5)


def test_r_multiple_sell_loser():
    c = NarratorComposer(StubProvider(), NarrativeStore(":memory:"))
    r = c._r_multiple(_ctx(side="SELL", entry_price=1.10000, stop_loss=1.10200,
                           exit_price=1.10200))  # stopped out → -1R
    assert r == pytest.approx(-1.0)


def test_r_multiple_returns_none_when_no_exit():
    c = NarratorComposer(StubProvider(), NarrativeStore(":memory:"))
    assert c._r_multiple(_ctx(exit_price=None)) is None


# ---------------------------------------------------------------- gather + narrate


def _seed_trade(db: Path, *, closed: bool = True) -> int:
    """Seed a trades row + matching trade_explanations + fills row.
    Returns the trade_id.
    """
    # Reuse the production schema by importing the stores so the schema migrations
    # all run before we INSERT.
    from src.execution.fills import FillStore
    from src.execution.journal import TradeJournal
    from src.explanations.store import TradeExplanationStore
    TradeJournal(db)
    TradeExplanationStore(db)
    FillStore(db)
    with sqlite3.connect(db) as c:
        c.execute(
            """INSERT INTO trades (id, symbol, side, lot_size, entry_price,
                exit_price, stop_loss, take_profit, strategy, status, opened_at,
                closed_at, pnl, close_reason)
               VALUES (1, 'EURUSD', 'BUY', 0.20, 1.10000, ?, 1.09800,
                       1.10500, 'MACrossover', ?, '2026-04-26T10:00:00+00:00',
                       ?, ?, ?)""",
            (
                1.10500 if closed else None,
                "CLOSED" if closed else "OPEN",
                "2026-04-26T11:30:00+00:00" if closed else None,
                100.0 if closed else 0.0,
                "target" if closed else None,
            ),
        )
        c.execute(
            """INSERT INTO trade_explanations (
                    trade_id, strategy, symbol, side,
                    signal_price, signal_stop, signal_target,
                    risk_reward, stop_distance_pips, lot_size, account_balance,
                    regime_label, allocator_role, allocator_weight,
                    ml_filter_passed, notes, opened_at)
               VALUES (1, 'MACrossover', 'EURUSD', 'BUY',
                       1.10000, 1.09800, 1.10500, 2.5, 20.0, 0.20, 10000.0,
                       'trend_up/normal', 'champion', 1.0, 1, '',
                       '2026-04-26T10:00:00+00:00')"""
        )
    return 1


def test_gather_returns_full_context(tmp_path):
    db = tmp_path / "trades.db"
    _seed_trade(db)
    c = NarratorComposer(StubProvider(), NarrativeStore(db), db_path=db)
    ctx = c.gather(1)
    assert ctx is not None
    assert ctx.symbol == "EURUSD"
    assert ctx.regime_label == "trend_up/normal"
    assert ctx.risk_reward == 2.5
    assert ctx.ml_filter_passed is True


def test_gather_returns_none_for_missing_trade(tmp_path):
    db = tmp_path / "trades.db"
    _seed_trade(db)
    c = NarratorComposer(StubProvider(), NarrativeStore(db), db_path=db)
    assert c.gather(999) is None


def test_narrate_writes_row_for_closed_trade(tmp_path):
    db = tmp_path / "trades.db"
    _seed_trade(db)
    store = NarrativeStore(db)
    c = NarratorComposer(StubProvider(), store, db_path=db)
    n = c.narrate(1)
    assert n is not None
    assert n.provider == "stub"
    assert "EURUSD" in n.narrative
    # Idempotent — second call returns the stored row, doesn't re-call provider.
    n2 = c.narrate(1)
    assert n2.created_at == n.created_at


def test_narrate_skips_open_trade(tmp_path):
    db = tmp_path / "trades.db"
    _seed_trade(db, closed=False)
    c = NarratorComposer(StubProvider(), NarrativeStore(db), db_path=db)
    assert c.narrate(1) is None


def test_narrate_force_overwrites(tmp_path):
    db = tmp_path / "trades.db"
    _seed_trade(db)
    store = NarrativeStore(db)

    class Counter(StubProvider):
        def __init__(self):
            self.calls = 0
        def complete(self, system, user, max_tokens=350):
            self.calls += 1
            return LLMResponse(text=f"call #{self.calls}", model="stub")

    p = Counter()
    c = NarratorComposer(p, store, db_path=db)
    c.narrate(1)
    c.narrate(1, force=True)
    assert p.calls == 2
    assert store.get(1).narrative == "call #2"


def test_narrate_swallows_provider_failure(tmp_path):
    db = tmp_path / "trades.db"
    _seed_trade(db)
    store = NarrativeStore(db)

    class Boom:
        name = "boom"
        def complete(self, system, user, max_tokens=350):
            raise RuntimeError("api 503")

    c = NarratorComposer(Boom(), store, db_path=db)
    assert c.narrate(1) is None
    assert store.get(1) is None


# ---------------------------------------------------------------- API


def test_narrative_endpoint_returns_404_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "x" * 64)
    db = tmp_path / "trades.db"
    store = NarrativeStore(db)

    from src.api import auth as auth_module
    from src.api import server as server_module
    monkeypatch.setattr(server_module, "_DB", db)
    monkeypatch.setattr(server_module, "narrative_store", store)
    server_module.app.dependency_overrides[auth_module.current_user] = lambda: {
        "username": "t", "role": "admin",
    }
    try:
        client = TestClient(server_module.app)
        r = client.get("/trades/123/narrative")
        assert r.status_code == 404
    finally:
        server_module.app.dependency_overrides.clear()


def test_narrative_endpoint_returns_stored_narrative(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "x" * 64)
    db = tmp_path / "trades.db"
    store = NarrativeStore(db)
    store.write(TradeNarrative(
        trade_id=42,
        narrative="bot caught the breakout, target hit at +2R",
        provider="stub",
        model="stub",
        prompt_tokens=20,
        output_tokens=15,
        created_at=_now().isoformat(),
    ))

    from src.api import auth as auth_module
    from src.api import server as server_module
    monkeypatch.setattr(server_module, "_DB", db)
    monkeypatch.setattr(server_module, "narrative_store", store)
    server_module.app.dependency_overrides[auth_module.current_user] = lambda: {
        "username": "t", "role": "admin",
    }
    try:
        client = TestClient(server_module.app)
        r = client.get("/trades/42/narrative")
        assert r.status_code == 200
        body = r.json()
        assert body["narrative"].startswith("bot caught")
        assert body["provider"] == "stub"
    finally:
        server_module.app.dependency_overrides.clear()
