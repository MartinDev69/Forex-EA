"""Auth unit + integration tests.

Covers:
- bcrypt hash/verify round-trip
- JWT encode/decode, tamper/expiry rejection
- rate limiter sliding window
- /auth/login happy + wrong-pw + 429 paths
- protected endpoint returns 401 without token, 200 with valid token
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("AUTH_SECRET", "test-secret-at-least-32-characters-long-xxxx")


@pytest.fixture
def fresh_server(tmp_path: Path, monkeypatch):
    """Reload the server module with an isolated DB + limiter per test."""
    import importlib

    from src.api import auth as auth_module
    from src.api import server as server_module

    db = tmp_path / "trades.db"
    from src.api.users import UserStore
    from src.execution.journal import TradeJournal
    from src.execution.strategy_toggles import StrategyToggleStore

    monkeypatch.setattr(server_module, "journal", TradeJournal(db))
    monkeypatch.setattr(server_module, "toggle_store", StrategyToggleStore(db))
    monkeypatch.setattr(server_module, "user_store", UserStore(db))
    # Fresh limiter so one test's failures don't leak into another.
    monkeypatch.setattr(auth_module, "_rate_limiter", auth_module.LoginRateLimiter())

    return server_module


def test_hash_and_verify_round_trip():
    from src.api.auth import hash_password, verify_password

    h = hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong", h)


def test_jwt_round_trip_and_tamper():
    from src.api.auth import create_token, decode_token

    tok, exp = create_token("Admi8X")
    assert decode_token(tok) == {"username": "Admi8X", "role": "admin"}
    assert exp > time.time()

    # Mutate middle-section byte -> should refuse to decode.
    parts = tok.split(".")
    bad = ".".join([parts[0], parts[1][:-1] + ("A" if parts[1][-1] != "A" else "B"), parts[2]])
    with pytest.raises(HTTPException) as e:
        decode_token(bad)
    assert e.value.status_code == 401


def test_jwt_expiry_rejected():
    from src.api.auth import decode_token, _secret

    payload = {"sub": "Admi8X", "iat": 0, "exp": 1}
    token = jwt.encode(payload, _secret(), algorithm="HS256")
    with pytest.raises(HTTPException) as e:
        decode_token(token)
    assert "expired" in e.value.detail


def test_rate_limiter_opens_after_window(monkeypatch):
    from src.api.auth import LoginRateLimiter

    rl = LoginRateLimiter(max_attempts=3, window_s=60)
    for _ in range(3):
        rl.check("1.2.3.4")
        rl.record("1.2.3.4")
    with pytest.raises(HTTPException) as e:
        rl.check("1.2.3.4")
    assert e.value.status_code == 429

    # A different IP is unaffected.
    rl.check("5.6.7.8")

    # Reset clears the record.
    rl.reset("1.2.3.4")
    rl.check("1.2.3.4")


def test_login_success_and_me(fresh_server):
    from src.api.auth import hash_password

    fresh_server.user_store.create_admin(hash_password("hunter2hunter2"))
    client = TestClient(fresh_server.app)

    r = client.post("/auth/login", json={"username": "Admi8X", "password": "hunter2hunter2"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "Admi8X"
    assert body["token_type"] == "bearer"
    token = body["access_token"]

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json() == {"username": "Admi8X", "role": "admin"}


def test_login_wrong_password_returns_401(fresh_server):
    from src.api.auth import hash_password

    fresh_server.user_store.create_admin(hash_password("hunter2hunter2"))
    client = TestClient(fresh_server.app)

    r = client.post("/auth/login", json={"username": "Admi8X", "password": "bad"})
    assert r.status_code == 401


def test_login_unknown_user_returns_401_no_enumeration(fresh_server):
    client = TestClient(fresh_server.app)
    r = client.post("/auth/login", json={"username": "ghost", "password": "whatever"})
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"].lower()


def test_login_rate_limit_kicks_in_after_5_attempts(fresh_server):
    client = TestClient(fresh_server.app)
    for _ in range(5):
        r = client.post("/auth/login", json={"username": "Admi8X", "password": "nope"})
        assert r.status_code == 401
    r = client.post("/auth/login", json={"username": "Admi8X", "password": "nope"})
    assert r.status_code == 429


def test_protected_endpoint_requires_token(fresh_server):
    client = TestClient(fresh_server.app)
    assert client.get("/status").status_code == 401
    assert client.get("/account").status_code == 401
    assert client.get("/strategies").status_code == 401
    assert client.get("/trades").status_code == 401
    assert client.post("/bot/start").status_code == 401


def test_protected_endpoint_accepts_valid_token(fresh_server):
    from src.api.auth import hash_password

    fresh_server.user_store.create_admin(hash_password("hunter2hunter2"))
    client = TestClient(fresh_server.app)
    tok = client.post("/auth/login",
                      json={"username": "Admi8X", "password": "hunter2hunter2"}).json()["access_token"]

    r = client.get("/status", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "running" in r.json()


def test_health_stays_unauthenticated(fresh_server):
    client = TestClient(fresh_server.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_auth_secret_missing_fails_closed(monkeypatch):
    from src.api import auth as auth_module

    monkeypatch.delenv("AUTH_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="AUTH_SECRET"):
        auth_module._secret()


def test_user_store_create_and_get(tmp_path: Path):
    from src.api.users import UserStore

    store = UserStore(tmp_path / "users.db")
    store.create("alice", "hash1")
    assert store.exists("alice")
    assert store.get_hash("alice") == "hash1"
    assert store.list_usernames() == ["alice"]
    assert store.update_password("alice", "hash2") is True
    assert store.get_hash("alice") == "hash2"
    assert store.update_password("ghost", "x") is False
