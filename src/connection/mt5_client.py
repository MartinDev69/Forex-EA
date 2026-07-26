"""MT5 connection wrapper.

The `MetaTrader5` package only installs on Windows. On macOS/Linux the import
will fail — the robot is intended to run live on a Windows VPS. Local development
(backtesting, strategy work, mobile UI) does not require MT5 to import.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    import MetaTrader5 as mt5
else:
    try:
        import MetaTrader5 as mt5
    except ImportError:
        mt5 = None


TIMEFRAME_MAP = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 16385, "H4": 16388, "D1": 16408, "W1": 32769, "MN1": 49153,
}


@dataclass
class AccountInfo:
    login: int
    balance: float
    equity: float
    currency: str
    leverage: int
    server: str


class MT5Client:
    def __init__(
        self,
        login: int,
        password: str,
        server: str,
        path: str | None = None,
    ) -> None:
        if mt5 is None:
            raise RuntimeError(
                "MetaTrader5 package not installed. This only works on Windows. "
                "For local dev on macOS/Linux, mock this client or run on a Windows VPS."
            )
        self.login = login
        self.password = password
        self.server = server
        self.path = path
        self._connected = False

    def connect(self) -> bool:
        kwargs = {"login": self.login, "password": self.password, "server": self.server}
        if self.path:
            kwargs["path"] = self.path
        if not mt5.initialize(**kwargs):
            raise ConnectionError(f"MT5 initialize failed: {mt5.last_error()}")

        # initialize() succeeds by ATTACHING to an already-running terminal, and
        # in that case it can ignore the credentials above entirely -- leaving us
        # authenticated as whatever account that terminal happens to be logged
        # into. Without the checks below, wrong creds silently "connect" to the
        # wrong account: /broker/test reported ok=True for login=1/password='p',
        # and the bot would have traded whatever session it found. Authenticate
        # explicitly, then prove the session is the account we asked for.
        if not mt5.login(self.login, password=self.password, server=self.server):
            err = mt5.last_error()
            mt5.shutdown()
            raise ConnectionError(
                f"MT5 login failed for {self.login}@{self.server}: {err}"
            )
        info = mt5.account_info()
        if info is None:
            err = mt5.last_error()
            mt5.shutdown()
            raise ConnectionError(f"MT5 account_info failed after login: {err}")
        if int(info.login) != int(self.login):
            actual = info.login
            mt5.shutdown()
            raise ConnectionError(
                f"MT5 session belongs to account {actual}, expected {self.login} "
                "— refusing to trade an unintended account."
            )

        self._connected = True
        return True

    def disconnect(self) -> None:
        if self._connected:
            mt5.shutdown()
            self._connected = False

    def account_info(self) -> AccountInfo:
        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"account_info failed: {mt5.last_error()}")
        return AccountInfo(
            login=info.login,
            balance=info.balance,
            equity=info.equity,
            currency=info.currency,
            leverage=info.leverage,
            server=info.server,
        )

    def rates(self, symbol: str, timeframe: str, count: int = 500) -> pd.DataFrame:
        tf = TIMEFRAME_MAP.get(timeframe.upper())
        if tf is None:
            raise ValueError(f"Unknown timeframe: {timeframe}")
        bars = mt5.copy_rates_from(symbol, tf, datetime.now(), count)
        if bars is None:
            raise RuntimeError(f"copy_rates_from failed: {mt5.last_error()}")
        df = pd.DataFrame(bars)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df.set_index("time")

    def __enter__(self) -> "MT5Client":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()
