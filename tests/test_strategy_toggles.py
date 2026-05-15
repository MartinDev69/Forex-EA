"""StrategyToggleStore — persistence and API wiring."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.execution.strategy_toggles import StrategyToggleStore

os.environ.setdefault("AUTH_SECRET", "test-secret-at-least-32-characters-long-xxxx")


def test_initialize_defaults_is_idempotent(tmp_path: Path):
    store = StrategyToggleStore(tmp_path / "trades.db")
    store.initialize_defaults({"alpha": True, "beta": False})
    # Second call with different values must NOT override existing flags.
    store.initialize_defaults({"alpha": False, "beta": True, "gamma": True})
    flags = store.list()
    assert flags == {"alpha": True, "beta": False, "gamma": True}


def test_set_and_toggle(tmp_path: Path):
    store = StrategyToggleStore(tmp_path / "trades.db")
    store.set("alpha", True)
    assert store.is_enabled("alpha") is True
    assert store.toggle("alpha") is False
    assert store.is_enabled("alpha") is False


def test_toggle_unknown_raises(tmp_path: Path):
    store = StrategyToggleStore(tmp_path / "trades.db")
    with pytest.raises(KeyError):
        store.toggle("ghost_strategy")


def test_two_stores_share_state(tmp_path: Path):
    """Simulates API process and bot process seeing the same SQLite file."""
    db = tmp_path / "trades.db"
    api_side = StrategyToggleStore(db)
    api_side.set("ma_crossover", True)

    bot_side = StrategyToggleStore(db)
    assert bot_side.is_enabled("ma_crossover") is True

    api_side.toggle("ma_crossover")
    assert bot_side.is_enabled("ma_crossover") is False


def test_api_toggle_persists_to_store(tmp_path: Path, monkeypatch):
    """POST /strategies/{name}/toggle must flip the flag in SQLite."""
    from src.api import server as server_module
    from src.api.auth import current_user, require_admin

    db = tmp_path / "trades.db"
    fresh_store = StrategyToggleStore(db)
    fresh_store.initialize_defaults({"ma_crossover": True, "rsi_mean_reversion": False})
    monkeypatch.setattr(server_module, "toggle_store", fresh_store)
    stub = lambda: {"username": "test", "role": "admin"}
    server_module.app.dependency_overrides[current_user] = stub
    server_module.app.dependency_overrides[require_admin] = stub

    try:
        client = TestClient(server_module.app)

        listed = client.get("/strategies").json()
        names = {s["name"]: s["enabled"] for s in listed}
        assert names["ma_crossover"] is True
        assert names["rsi_mean_reversion"] is False

        r = client.post("/strategies/ma_crossover/toggle")
        assert r.status_code == 200
        # Response shape gained `mode` and `user_copyable` since this
        # test was first written. Pin the fields we actually care about
        # rather than the full body so future additions don't re-break.
        body = r.json()
        assert body["name"] == "ma_crossover"
        assert body["enabled"] is False

        # The SQLite file should reflect the new value, independent of the API instance.
        assert StrategyToggleStore(db).is_enabled("ma_crossover") is False
    finally:
        server_module.app.dependency_overrides.clear()


def test_api_toggle_unknown_returns_404(tmp_path: Path, monkeypatch):
    from src.api import server as server_module
    from src.api.auth import current_user, require_admin

    fresh_store = StrategyToggleStore(tmp_path / "trades.db")
    monkeypatch.setattr(server_module, "toggle_store", fresh_store)
    stub = lambda: {"username": "test", "role": "admin"}
    server_module.app.dependency_overrides[current_user] = stub
    server_module.app.dependency_overrides[require_admin] = stub

    try:
        client = TestClient(server_module.app)
        r = client.post("/strategies/does_not_exist/toggle")
        assert r.status_code == 404
    finally:
        server_module.app.dependency_overrides.clear()
