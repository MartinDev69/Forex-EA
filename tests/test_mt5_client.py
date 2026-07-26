"""MT5Client.connect() — credential verification.

The regression these guard: mt5.initialize() succeeds by ATTACHING to an
already-running terminal, and in that case ignores the login/password/server
handed to it. Without an explicit login + identity check, connect() reported
success while the session belonged to whatever account the terminal was already
signed into — /broker/test returned ok=True for login=1/password='p', and the
bot would have traded that unintended account.
"""
from __future__ import annotations

import pytest

import src.connection.mt5_client as mt5_client_module
from src.connection.mt5_client import MT5Client


class _FakeMT5:
    """Mimics the MetaTrader5 module. `session_login` is the account the
    terminal is really signed into, which need not match what we asked for.
    """

    def __init__(
        self,
        session_login: int,
        login_ok: bool = True,
        init_ok: bool = True,
        switch_on_login: bool = True,
    ):
        self.session_login = session_login
        self.login_ok = login_ok
        self.init_ok = init_ok
        # False models a terminal that reports login success without actually
        # switching accounts -- the case the identity assertion must catch.
        self.switch_on_login = switch_on_login
        self.shutdown_calls = 0
        self.login_calls: list[tuple] = []

    def initialize(self, **kwargs):
        return self.init_ok

    def login(self, login, password=None, server=None):
        self.login_calls.append((login, password, server))
        if not self.login_ok:
            return False
        if self.switch_on_login:
            self.session_login = login
        return True

    def account_info(self):
        class _Info:
            login = self.session_login
            balance = 1000.0
            equity = 1000.0
            currency = "USD"
            leverage = 500
            server = "Some-Server"
        return _Info()

    def shutdown(self):
        self.shutdown_calls += 1

    def last_error(self):
        return (-1, "fake error")


@pytest.fixture
def fake_mt5(monkeypatch):
    def _install(**kwargs):
        fake = _FakeMT5(**kwargs)
        monkeypatch.setattr(mt5_client_module, "mt5", fake)
        return fake
    return _install


def _client(login=41183004):
    return MT5Client(login=login, password="pw", server="Deriv-Demo")


def test_connect_succeeds_when_session_matches(fake_mt5):
    fake = fake_mt5(session_login=41183004)
    assert _client().connect() is True
    assert fake.login_calls == [(41183004, "pw", "Deriv-Demo")]
    assert fake.shutdown_calls == 0


def test_connect_calls_login_not_just_initialize(fake_mt5):
    """initialize() alone must not be treated as authentication."""
    fake = fake_mt5(session_login=41183004)
    _client().connect()
    assert fake.login_calls, "connect() must explicitly authenticate via mt5.login()"


def test_connect_rejects_wrong_account(fake_mt5):
    """The core regression: terminal signed into a different account."""
    fake = fake_mt5(session_login=99999999, login_ok=False)
    with pytest.raises(ConnectionError, match="login failed"):
        _client().connect()
    assert fake.shutdown_calls == 1, "must not leave a half-open session behind"


def test_connect_rejects_identity_mismatch_even_if_login_returns_true(fake_mt5):
    """Belt-and-braces: login() reported success but the session never switched,
    so account_info() still disagrees with what was requested."""
    fake = fake_mt5(session_login=99999999, switch_on_login=False)
    with pytest.raises(ConnectionError, match="belongs to account 99999999"):
        _client().connect()
    assert fake.shutdown_calls == 1


def test_connect_raises_when_initialize_fails(fake_mt5):
    fake = fake_mt5(session_login=41183004, init_ok=False)
    with pytest.raises(ConnectionError, match="initialize failed"):
        _client().connect()
    assert not fake.login_calls, "must not attempt login when initialize() failed"
