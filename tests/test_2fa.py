"""Tests for TOTP primitive, encrypted store, enrollment endpoints, and the
require_2fa gate on destructive operations."""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("AUTH_SECRET", "test-secret-at-least-32-characters-long-xxxx")
_SECRET = os.environ["AUTH_SECRET"]


# ---------- TOTP primitive ----------

def test_generate_secret_is_base32_and_decodable():
    from src.api.totp import _decode_secret, generate_secret

    s = generate_secret()
    # Base32 alphabet only — no padding because we strip it for manual entry.
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in s)
    # 20-byte secret → 32 base32 chars.
    assert len(s) == 32
    # Round-trip decodes cleanly even without padding.
    raw = _decode_secret(s)
    assert len(raw) == 20


def test_generate_code_is_six_digits():
    from src.api.totp import generate_code, generate_secret

    secret = generate_secret()
    code = generate_code(secret, at=1700000000.0)
    assert len(code) == 6
    assert code.isdigit()


def test_generate_code_matches_rfc_6238_test_vector():
    """RFC 6238 vector: secret 12345678901234567890 (ASCII) at T=59 → 94287082
    for SHA-256/8 digits. We use SHA-1/6 digits, so use a known SHA-1/6 vector
    derived from the same secret: at T=59 with SHA-1 → 287082 (last 6 of the
    8-digit RFC value). Verifying our 6-digit truncation matches."""
    import base64

    from src.api.totp import generate_code

    raw = b"12345678901234567890"
    secret_b32 = base64.b32encode(raw).decode("ascii").rstrip("=")
    # T=59 → t_step = 1; RFC SHA-1 8-digit value is 94287082, last 6 = 287082.
    code = generate_code(secret_b32, at=59.0)
    assert code == "287082"


def test_verify_code_accepts_drift_window():
    from src.api.totp import generate_code, generate_secret, verify_code

    secret = generate_secret()
    now = 1700000000.0
    # Code from one step ago is still accepted.
    prev = generate_code(secret, at=now - 30)
    assert verify_code(secret, prev, at=now) is True
    # Code from one step ahead is also accepted.
    nxt = generate_code(secret, at=now + 30)
    assert verify_code(secret, nxt, at=now) is True
    # Code from two steps ago is rejected.
    far = generate_code(secret, at=now - 60)
    assert verify_code(secret, far, at=now) is False


def test_verify_code_rejects_garbage():
    from src.api.totp import generate_secret, verify_code

    secret = generate_secret()
    assert verify_code(secret, "") is False
    assert verify_code(secret, "abcdef") is False
    assert verify_code(secret, "12345") is False  # too short
    assert verify_code(secret, "1234567") is False  # too long
    assert verify_code(secret, "000000") is False  # vanishingly unlikely match


def test_provisioning_uri_format():
    from src.api.totp import provisioning_uri

    uri = provisioning_uri("ABC234", account="alice@example.com", issuer="Forex-EA")
    assert uri.startswith("otpauth://totp/Forex-EA:")
    assert "secret=ABC234" in uri
    assert "issuer=Forex-EA" in uri
    assert "algorithm=SHA1" in uri
    assert "digits=6" in uri
    assert "period=30" in uri


# ---------- TOTPStore ----------

def test_store_encrypts_pending_at_rest(tmp_path: Path):
    from src.api.totp_store import TOTPStore

    db = tmp_path / "trades.db"
    store = TOTPStore(db, secret=_SECRET)
    store.stage_pending("alice", "PLAINTEXTSECRET234")
    assert store.get_pending_secret("alice") == "PLAINTEXTSECRET234"

    with sqlite3.connect(db) as c:
        row = c.execute(
            "SELECT pending_secret_enc FROM user_totp WHERE username = ?", ("alice",)
        ).fetchone()
    assert "PLAINTEXTSECRET234" not in row[0]


def test_store_pending_does_not_overwrite_active(tmp_path: Path):
    """Re-running enroll while already 2FA'd keeps the working secret active
    until the new one is confirmed — otherwise a half-finished re-enrollment
    locks the operator out."""
    from src.api.totp_store import TOTPStore

    store = TOTPStore(tmp_path / "trades.db", secret=_SECRET)
    store.stage_pending("alice", "PENDING1")
    store.activate("alice", "PENDING1")
    store.stage_pending("alice", "PENDING2")  # start re-enrollment
    assert store.get_active_secret("alice") == "PENDING1"
    assert store.get_pending_secret("alice") == "PENDING2"


def test_store_activate_clears_pending_and_preserves_enrolled_at(tmp_path: Path):
    from src.api.totp_store import TOTPStore

    store = TOTPStore(tmp_path / "trades.db", secret=_SECRET)
    store.stage_pending("alice", "S1")
    store.activate("alice", "S1")
    first = store.status("alice").enrolled_at
    assert first is not None
    assert store.get_pending_secret("alice") is None

    time.sleep(0.01)
    store.stage_pending("alice", "S2")
    store.activate("alice", "S2")
    # enrolled_at is the original enrollment, not the rotation moment.
    assert store.status("alice").enrolled_at == first
    assert store.get_active_secret("alice") == "S2"


def test_store_disable_removes_row(tmp_path: Path):
    from src.api.totp_store import TOTPStore

    store = TOTPStore(tmp_path / "trades.db", secret=_SECRET)
    store.stage_pending("alice", "S1")
    store.activate("alice", "S1")
    assert store.disable("alice") is True
    assert store.is_enabled("alice") is False
    assert store.disable("alice") is False  # idempotent


def test_store_status_for_unknown_user(tmp_path: Path):
    from src.api.totp_store import TOTPStore

    store = TOTPStore(tmp_path / "trades.db", secret=_SECRET)
    s = store.status("nobody")
    assert s.enabled is False
    assert s.pending is False
    assert s.enrolled_at is None


def test_store_rotated_secret_cannot_decrypt(tmp_path: Path):
    from src.api.totp_store import TOTPStore

    db = tmp_path / "trades.db"
    a = TOTPStore(db, secret=_SECRET)
    a.stage_pending("alice", "ORIGINAL")
    a.activate("alice", "ORIGINAL")

    b = TOTPStore(db, secret="different-secret-32-chars-at-least-yyy")
    with pytest.raises(ValueError, match="rotated AUTH_SECRET"):
        b.get_active_secret("alice")


def test_store_isolated_from_broker_config_keys(tmp_path: Path):
    """Different salt → key for TOTP differs from broker_config's, so leaking
    one path's encrypted blobs doesn't compromise the other."""
    from src.api.broker_config import BrokerConfig, BrokerConfigStore
    from src.api.totp_store import TOTPStore

    db = tmp_path / "trades.db"
    bc = BrokerConfigStore(db, secret=_SECRET)
    bc.save("alice", BrokerConfig(broker="xm", login=1, password="brokerpw", server="x"))

    totp = TOTPStore(db, secret=_SECRET)
    totp.stage_pending("alice", "TOTPPLAINTEXT")

    with sqlite3.connect(db) as c:
        bc_row = c.execute(
            "SELECT password_enc FROM broker_config WHERE username = ?", ("alice",)
        ).fetchone()
        totp_row = c.execute(
            "SELECT pending_secret_enc FROM user_totp WHERE username = ?", ("alice",)
        ).fetchone()
    # Both encrypted, neither holds the other's plaintext.
    assert "brokerpw" not in bc_row[0]
    assert "TOTPPLAINTEXT" not in totp_row[0]


def test_store_rejects_short_secret(tmp_path: Path):
    from src.api.totp_store import TOTPStore

    with pytest.raises(ValueError, match="32"):
        TOTPStore(tmp_path / "x.db", secret="short")


# ---------- API enrollment endpoints ----------

@pytest.fixture
def fresh_server(tmp_path: Path, monkeypatch):
    from src.api import auth as auth_module
    from src.api import server as server_module
    from src.api.broker_config import BrokerConfigStore
    from src.api.totp_store import TOTPStore
    from src.api.users import UserStore
    from src.execution.journal import TradeJournal
    from src.execution.strategy_toggles import StrategyToggleStore

    db = tmp_path / "trades.db"
    monkeypatch.setattr(server_module, "_DB", db)
    monkeypatch.setattr(server_module, "journal", TradeJournal(db))
    monkeypatch.setattr(server_module, "toggle_store", StrategyToggleStore(db))
    monkeypatch.setattr(server_module, "user_store", UserStore(db))
    monkeypatch.setattr(server_module, "_broker_config_store",
                        BrokerConfigStore(db, secret=_SECRET))
    monkeypatch.setattr(server_module, "_totp_store",
                        TOTPStore(db, secret=_SECRET))
    monkeypatch.setattr(auth_module, "_rate_limiter", auth_module.LoginRateLimiter())

    stub = lambda: {"username": "alice", "role": "admin"}
    server_module.app.dependency_overrides[auth_module.current_user] = stub
    server_module.app.dependency_overrides[auth_module.require_admin] = stub
    yield server_module
    server_module.app.dependency_overrides.clear()


def test_status_initially_disabled(fresh_server):
    client = TestClient(fresh_server.app)
    r = client.get("/auth/2fa/status")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "pending": False, "enrolled_at": None}


def test_enroll_returns_secret_and_uri(fresh_server):
    client = TestClient(fresh_server.app)
    r = client.post("/auth/2fa/enroll")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["secret"]
    assert body["provisioning_uri"].startswith("otpauth://totp/Forex-EA:alice")
    assert f"secret={body['secret']}" in body["provisioning_uri"]

    # Status now reflects pending.
    s = client.get("/auth/2fa/status").json()
    assert s == {"enabled": False, "pending": True, "enrolled_at": None}


def test_confirm_activates_with_valid_code(fresh_server):
    from src.api.totp import generate_code

    client = TestClient(fresh_server.app)
    secret = client.post("/auth/2fa/enroll").json()["secret"]
    code = generate_code(secret)
    r = client.post("/auth/2fa/confirm", json={"code": code})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["pending"] is False
    assert body["enrolled_at"] is not None


def test_confirm_rejects_invalid_code(fresh_server):
    client = TestClient(fresh_server.app)
    client.post("/auth/2fa/enroll")
    r = client.post("/auth/2fa/confirm", json={"code": "000000"})
    assert r.status_code == 401
    # Pending remains so caller can retry.
    assert client.get("/auth/2fa/status").json()["pending"] is True


def test_confirm_without_pending_is_400(fresh_server):
    client = TestClient(fresh_server.app)
    r = client.post("/auth/2fa/confirm", json={"code": "123456"})
    assert r.status_code == 400


def test_disable_requires_valid_code(fresh_server):
    from src.api.totp import generate_code

    client = TestClient(fresh_server.app)
    secret = client.post("/auth/2fa/enroll").json()["secret"]
    client.post("/auth/2fa/confirm", json={"code": generate_code(secret)})

    bad = client.post("/auth/2fa/disable", json={"code": "000000"})
    assert bad.status_code == 401
    assert client.get("/auth/2fa/status").json()["enabled"] is True

    good = client.post("/auth/2fa/disable", json={"code": generate_code(secret)})
    assert good.status_code == 200
    assert good.json()["enabled"] is False


def test_disable_when_not_enrolled_is_400(fresh_server):
    client = TestClient(fresh_server.app)
    r = client.post("/auth/2fa/disable", json={"code": "123456"})
    assert r.status_code == 400


def test_reenroll_keeps_active_secret_until_new_code_confirmed(fresh_server):
    from src.api.totp import generate_code

    client = TestClient(fresh_server.app)
    s1 = client.post("/auth/2fa/enroll").json()["secret"]
    client.post("/auth/2fa/confirm", json={"code": generate_code(s1)})

    # Start a re-enrollment; original code should still pass require_2fa-style checks.
    s2 = client.post("/auth/2fa/enroll").json()["secret"]
    assert s2 != s1
    # Disable still works with the *original* secret because s2 is only pending.
    r = client.post("/auth/2fa/disable", json={"code": generate_code(s1)})
    assert r.status_code == 200


# ---------- require_2fa gate ----------

def test_destructive_op_open_when_2fa_off(fresh_server):
    """Without 2FA enrolled, destructive endpoints work as before."""
    client = TestClient(fresh_server.app)
    r = client.post("/bot/start")
    assert r.status_code == 200
    assert r.json() == {"status": "started"}


def test_destructive_op_demands_code_when_2fa_on(fresh_server):
    from src.api.totp import generate_code

    client = TestClient(fresh_server.app)
    secret = client.post("/auth/2fa/enroll").json()["secret"]
    client.post("/auth/2fa/confirm", json={"code": generate_code(secret)})

    # Missing header.
    r = client.post("/bot/start")
    assert r.status_code == 401
    assert "2FA" in r.json()["detail"]

    # Wrong code.
    r = client.post("/bot/start", headers={"X-2FA-Code": "000000"})
    assert r.status_code == 401

    # Correct code.
    r = client.post("/bot/start", headers={"X-2FA-Code": generate_code(secret)})
    assert r.status_code == 200


def test_strategy_toggle_gated_by_2fa(fresh_server):
    """Spot-check another destructive route — toggling a strategy."""
    from src.api.totp import generate_code

    client = TestClient(fresh_server.app)
    fresh_server.toggle_store.initialize_defaults({"ma_crossover": False})
    secret = client.post("/auth/2fa/enroll").json()["secret"]
    client.post("/auth/2fa/confirm", json={"code": generate_code(secret)})

    r = client.post("/strategies/ma_crossover/toggle")
    assert r.status_code == 401

    r = client.post(
        "/strategies/ma_crossover/toggle",
        headers={"X-2FA-Code": generate_code(secret)},
    )
    assert r.status_code == 200


def test_read_only_endpoints_never_require_2fa(fresh_server):
    """Read-only routes stay open even after 2FA is enabled."""
    from src.api.totp import generate_code

    client = TestClient(fresh_server.app)
    secret = client.post("/auth/2fa/enroll").json()["secret"]
    client.post("/auth/2fa/confirm", json={"code": generate_code(secret)})

    assert client.get("/status").status_code == 200
    assert client.get("/account").status_code == 200
    assert client.get("/strategies").status_code == 200
    assert client.get("/auth/2fa/status").status_code == 200
