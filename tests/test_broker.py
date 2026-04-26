"""Broker presets + encrypted config store + API endpoint tests."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AUTH_SECRET", "test-secret-at-least-32-characters-long-xxxx")

_SECRET = os.environ["AUTH_SECRET"]


@pytest.fixture
def fresh_server(tmp_path: Path, monkeypatch):
    from src.api import auth as auth_module
    from src.api import server as server_module
    from src.api.broker_config import BrokerConfigStore
    from src.api.broker_status import BrokerStatusStore
    from src.api.users import UserStore
    from src.execution.journal import TradeJournal
    from src.execution.strategy_toggles import StrategyToggleStore

    db = tmp_path / "trades.db"
    monkeypatch.setattr(server_module, "journal", TradeJournal(db))
    monkeypatch.setattr(server_module, "toggle_store", StrategyToggleStore(db))
    monkeypatch.setattr(server_module, "user_store", UserStore(db))
    monkeypatch.setattr(server_module, "broker_status_store", BrokerStatusStore(db))
    # Reset the lazy broker config store + point it at the fresh DB.
    monkeypatch.setattr(server_module, "_broker_config_store",
                        BrokerConfigStore(db, secret=_SECRET))
    monkeypatch.setattr(auth_module, "_rate_limiter", auth_module.LoginRateLimiter())

    # Skip auth for most broker tests — auth is tested separately.
    stub = lambda: {"username": "test-user", "role": "admin"}
    server_module.app.dependency_overrides[auth_module.current_user] = stub
    server_module.app.dependency_overrides[auth_module.require_admin] = stub
    yield server_module
    server_module.app.dependency_overrides.clear()


# ---- Presets ----

def test_presets_include_required_brokers():
    from src.api.brokers import PRESET_BY_ID, as_dicts

    for expected in ("exness", "xm", "deriv_mt5", "icmarkets", "fbs", "pepperstone", "custom"):
        assert expected in PRESET_BY_ID

    dicts = as_dicts()
    assert all({"id", "display_name", "servers", "mt5_path_hint", "notes"} <= set(d) for d in dicts)
    # Exness should have multiple servers listed.
    exness = next(d for d in dicts if d["id"] == "exness")
    assert len(exness["servers"]) >= 3


# ---- BrokerConfigStore ----

def test_config_roundtrip_encrypts_password(tmp_path: Path):
    from src.api.broker_config import BrokerConfig, BrokerConfigStore

    store = BrokerConfigStore(tmp_path / "trades.db", secret=_SECRET)
    store.save("Admi8X", BrokerConfig(broker="exness", login=12345678, password="s3cret!",
                                      server="Exness-MT5Real5", mt5_path=""))
    cfg = store.get_decrypted("Admi8X")
    assert cfg is not None
    assert cfg.login == 12345678
    assert cfg.password == "s3cret!"
    assert cfg.server == "Exness-MT5Real5"

    # Raw DB should not contain the plaintext password.
    import sqlite3
    with sqlite3.connect(tmp_path / "trades.db") as c:
        row = c.execute(
            "SELECT password_enc FROM broker_config WHERE username = ?", ("Admi8X",)
        ).fetchone()
    assert "s3cret!" not in row[0]


def test_config_is_isolated_per_user(tmp_path: Path):
    """Each operator's broker credentials are invisible to other operators."""
    from src.api.broker_config import BrokerConfig, BrokerConfigStore

    store = BrokerConfigStore(tmp_path / "trades.db", secret=_SECRET)
    store.save("Admi8X", BrokerConfig(broker="exness", login=1, password="admin-pw",
                                      server="Exness-MT5Real5"))
    store.save("AD-AB12CD34", BrokerConfig(broker="xm", login=2, password="op-pw",
                                           server="XMGlobal-MT5"))
    assert store.get_decrypted("Admi8X").password == "admin-pw"
    assert store.get_decrypted("AD-AB12CD34").password == "op-pw"
    assert store.get_decrypted("AD-NOBODY00") is None
    # Clearing one account leaves the other untouched.
    store.clear("AD-AB12CD34")
    assert store.get_decrypted("AD-AB12CD34") is None
    assert store.get_decrypted("Admi8X").password == "admin-pw"


def test_config_different_secret_cannot_decrypt(tmp_path: Path):
    from src.api.broker_config import BrokerConfig, BrokerConfigStore

    store_a = BrokerConfigStore(tmp_path / "trades.db", secret=_SECRET)
    store_a.save("Admi8X", BrokerConfig(broker="exness", login=1, password="hunter2",
                                        server="Exness-MT5Real5"))
    store_b = BrokerConfigStore(tmp_path / "trades.db",
                                secret="different-secret-32-chars-at-least-!!!!")
    with pytest.raises(RuntimeError, match="AUTH_SECRET may have been rotated"):
        store_b.get_decrypted("Admi8X")


def test_config_masked_view_hides_password(tmp_path: Path):
    from src.api.broker_config import BrokerConfig, BrokerConfigStore

    store = BrokerConfigStore(tmp_path / "trades.db", secret=_SECRET)
    assert store.get_masked("Admi8X") is None
    store.save("Admi8X", BrokerConfig(broker="xm", login=99, password="pw", server="XMGlobal-MT5"))
    m = store.get_masked("Admi8X")
    assert m is not None
    assert "password" not in m
    assert m["password_set"] is True
    assert len(m["password_fingerprint"]) == 8
    assert m["login"] == 99


def test_config_clear(tmp_path: Path):
    from src.api.broker_config import BrokerConfig, BrokerConfigStore

    store = BrokerConfigStore(tmp_path / "trades.db", secret=_SECRET)
    store.save("Admi8X", BrokerConfig(broker="exness", login=1, password="p", server="s"))
    assert store.exists("Admi8X")
    assert store.clear("Admi8X") is True
    assert not store.exists("Admi8X")
    assert store.clear("Admi8X") is False  # idempotent


def test_config_rejects_short_secret(tmp_path: Path):
    from src.api.broker_config import BrokerConfigStore
    with pytest.raises(ValueError, match="32"):
        BrokerConfigStore(tmp_path / "x.db", secret="short")


# ---- BrokerStatusStore ----

def test_status_store_roundtrip(tmp_path: Path):
    from src.api.broker_status import BrokerStatusStore

    store = BrokerStatusStore(tmp_path / "trades.db")
    assert store.read() is None
    store.write(connected=True, broker="exness", server="Exness-MT5Real5",
                login=77, account_info={"balance": 1000.0, "currency": "USD"})
    s = store.read()
    assert s.connected is True
    assert s.broker == "exness"
    assert s.account_info == {"balance": 1000.0, "currency": "USD"}

    store.write(connected=False, last_error="timeout")
    s = store.read()
    assert s.connected is False
    assert s.last_error == "timeout"


# ---- API endpoints ----

def test_get_presets_endpoint(fresh_server):
    client = TestClient(fresh_server.app)
    r = client.get("/brokers")
    assert r.status_code == 200
    ids = {d["id"] for d in r.json()}
    assert {"exness", "xm", "deriv_mt5"}.issubset(ids)


def test_save_and_get_broker_config(fresh_server):
    client = TestClient(fresh_server.app)

    assert client.get("/broker/config").json() is None

    r = client.put("/broker/config", json={
        "broker": "exness", "login": 12345678, "password": "s3cret!",
        "server": "Exness-MT5Real5", "mt5_path": "",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["broker"] == "exness"
    assert body["login"] == 12345678
    assert body["password_set"] is True
    assert "password" not in body  # masked response

    r2 = client.get("/broker/config").json()
    assert r2["login"] == 12345678
    assert r2["server"] == "Exness-MT5Real5"


def test_put_rejects_unknown_broker(fresh_server):
    client = TestClient(fresh_server.app)
    r = client.put("/broker/config", json={
        "broker": "not-a-real-broker", "login": 1, "password": "p",
        "server": "x", "mt5_path": "",
    })
    assert r.status_code == 400


def test_delete_broker_config(fresh_server):
    client = TestClient(fresh_server.app)
    client.put("/broker/config", json={
        "broker": "xm", "login": 1, "password": "p",
        "server": "XMGlobal-MT5", "mt5_path": "",
    })
    assert client.delete("/broker/config").json() == {"removed": True}
    assert client.delete("/broker/config").json() == {"removed": False}
    assert client.get("/broker/config").json() is None


def test_broker_test_endpoint_without_mt5(fresh_server):
    """On macOS / Linux, MT5Client raises on construction — the endpoint should
    return ok=False with an explanation, not 500."""
    client = TestClient(fresh_server.app)
    r = client.post("/broker/test", json={
        "broker": "exness", "login": 1, "password": "p",
        "server": "Exness-MT5Real5", "mt5_path": "",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"]  # some explanation


def test_broker_status_endpoint_initially_disconnected(fresh_server):
    client = TestClient(fresh_server.app)
    r = client.get("/broker/status")
    assert r.status_code == 200
    assert r.json()["connected"] is False


def test_broker_status_reflects_heartbeat(fresh_server):
    fresh_server.broker_status_store.write(
        connected=True, broker="exness", server="Exness-MT5Real5", login=7,
        account_info={"balance": 5000.0, "equity": 5025.0, "currency": "USD", "leverage": 500},
    )
    client = TestClient(fresh_server.app)
    r = client.get("/broker/status").json()
    assert r["connected"] is True
    assert r["broker"] == "exness"
    assert r["account_info"]["balance"] == 5000.0
    assert r["stale_s"] is not None and r["stale_s"] >= 0


def test_broker_endpoints_require_auth(tmp_path: Path, monkeypatch):
    """Smoke test: without a token, broker endpoints 401."""
    from src.api import auth as auth_module
    from src.api import server as server_module
    from src.api.broker_config import BrokerConfigStore

    db = tmp_path / "trades.db"
    monkeypatch.setattr(server_module, "_broker_config_store",
                        BrokerConfigStore(db, secret=_SECRET))
    monkeypatch.setattr(auth_module, "_rate_limiter", auth_module.LoginRateLimiter())
    client = TestClient(server_module.app)
    assert client.get("/brokers").status_code == 401
    assert client.get("/broker/config").status_code == 401
    assert client.get("/broker/status").status_code == 401
    assert client.put("/broker/config", json={
        "broker": "exness", "login": 1, "password": "p",
        "server": "Exness-MT5Real5", "mt5_path": "",
    }).status_code == 401
