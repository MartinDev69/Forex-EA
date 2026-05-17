"""Position sizing: convert risk% + stop distance → lot size.

Pip size and pip value default to a small hardcoded fallback table so
unit tests and mock mode work without MT5. In live mode, ``main.py``
injects a ``PipResolver`` that queries the real MT5 terminal for every
symbol — the only correct answer when trading anything other than vanilla
4-decimal majors. Deriv volatility indices, exotic crosses, indices, and
crypto all have their own pip conventions and tick values; hardcoding
those is a foot-gun, MT5 already knows.
"""
from __future__ import annotations

from threading import Lock
from typing import Any, Optional


def _fallback_pip_size(symbol: str) -> float:
    s = symbol.upper()
    if s in {"XAUUSD", "GOLD", "XAGUSD", "SILVER"}:
        return 0.1
    if s.endswith("JPY"):
        return 0.01
    return 0.0001


def _fallback_pip_value(symbol: str) -> float:
    s = symbol.upper()
    if s in {"XAUUSD", "GOLD"}:
        return 10.0  # $1 move × 100 oz / lot
    if s.endswith("JPY"):
        return 9.0   # nominal — depends on JPY cross rate
    return 10.0


class PipResolver:
    """Per-symbol pip math driven by ``mt5.symbol_info``.

    Queries the live terminal once per symbol and caches the result. Pip
    size in MT5 convention is ``point * 10`` (a "point" is the smallest
    tradable increment, a "pip" is 10 of those — that's the convention every
    indicator in the codebase already assumes). Pip value uses
    ``trade_tick_value × (pip_size / trade_tick_size)`` so it works whether
    the broker quotes in account currency or via cross conversion.

    Falls back to the hardcoded heuristics if MT5 returns no data for the
    symbol — better to size a trade roughly than to crash the open path.
    """

    def __init__(self, mt5_module: Any) -> None:
        self._mt5 = mt5_module
        self._lock = Lock()
        self._size_cache: dict[str, float] = {}
        self._value_cache: dict[str, float] = {}

    def pip_size(self, symbol: str) -> float:
        with self._lock:
            if symbol in self._size_cache:
                return self._size_cache[symbol]
        size = self._fetch_size(symbol)
        if size is None or size <= 0:
            size = _fallback_pip_size(symbol)
        with self._lock:
            self._size_cache[symbol] = size
        return size

    def pip_value(self, symbol: str, lot_size: float = 1.0) -> float:
        with self._lock:
            if symbol in self._value_cache:
                return self._value_cache[symbol] * lot_size
        per_lot = self._fetch_value(symbol)
        if per_lot is None or per_lot <= 0:
            per_lot = _fallback_pip_value(symbol)
        with self._lock:
            self._value_cache[symbol] = per_lot
        return per_lot * lot_size

    def _ensure_selected(self, symbol: str) -> None:
        # symbol_info returns None for symbols not in Market Watch on some
        # brokers. Pushing it in via symbol_select is best-effort — failures
        # don't matter, the fetch attempt below will fall back.
        try:
            self._mt5.symbol_select(symbol, True)
        except Exception:
            pass

    def _fetch_size(self, symbol: str) -> Optional[float]:
        self._ensure_selected(symbol)
        try:
            info = self._mt5.symbol_info(symbol)
        except Exception:
            return None
        if info is None:
            return None
        point = float(getattr(info, "point", 0) or 0)
        if point <= 0:
            return None
        return point * 10  # MT5: pip = 10 points

    def _fetch_value(self, symbol: str) -> Optional[float]:
        self._ensure_selected(symbol)
        try:
            info = self._mt5.symbol_info(symbol)
        except Exception:
            return None
        if info is None:
            return None
        tick_value = float(getattr(info, "trade_tick_value", 0) or 0)
        tick_size = float(getattr(info, "trade_tick_size", 0) or 0)
        point = float(getattr(info, "point", 0) or 0)
        if tick_value <= 0 or point <= 0:
            return None
        # If the broker reports tick_size separately (some do, some leave
        # it at 0), use it; otherwise tick == point. Pip = 10 points either
        # way, so multiply through to get pip_value.
        ticks_per_pip = 10.0 if tick_size <= 0 else (point * 10) / tick_size
        return tick_value * ticks_per_pip


# Module-level singleton — main.py installs one after MT5 connects, mock
# mode and tests leave it as None so the fallback table is used. Threading
# isn't a real concern here (set once at startup) but a Lock is cheap.
_resolver: Optional[PipResolver] = None
_install_lock = Lock()


def set_resolver(resolver: PipResolver | None) -> None:
    """Install the live pip resolver. Pass None to clear (testing)."""
    global _resolver
    with _install_lock:
        _resolver = resolver


def pip_size(symbol: str) -> float:
    """Price delta corresponding to one pip for ``symbol``."""
    r = _resolver
    if r is not None:
        return r.pip_size(symbol)
    return _fallback_pip_size(symbol)


def pip_value(symbol: str, lot_size: float = 1.0) -> float:
    """Approximate USD pip value per lot for ``symbol``."""
    r = _resolver
    if r is not None:
        return r.pip_value(symbol, lot_size)
    return _fallback_pip_value(symbol) * lot_size


def lot_size_from_risk(
    account_balance: float,
    risk_pct: float,
    stop_distance_pips: float,
    symbol: str,
    min_lot: float = 0.01,
    max_lot: float = 100.0,
    lot_step: float = 0.01,
) -> float:
    """Return lot size that risks ``risk_pct`` of balance given stop distance.

    risk_amount = balance × risk_pct
    lots = risk_amount / (stop_distance_pips × pip_value_per_lot)

    Returns ``0`` when the math says less than ``min_lot`` — the bot
    treats 0 as "skip this signal". The previous version floored to
    min_lot, which over-risked small accounts: a 0.5% intended risk
    on a $500 account with a wide stop could land at 0.01 lots
    delivering ~2% actual risk per trade. For prop-firm accounts that
    quickly chews through the daily-loss budget. Skipping is the safer
    default; operators who need every signal can dial up risk_per_trade
    or down the stop distance instead.
    """
    if stop_distance_pips <= 0:
        raise ValueError("stop_distance_pips must be > 0")
    if not 0 < risk_pct < 1:
        raise ValueError("risk_pct must be between 0 and 1 (e.g. 0.01 for 1%)")

    risk_amount = account_balance * risk_pct
    per_lot_risk = stop_distance_pips * pip_value(symbol)
    raw_lots = risk_amount / per_lot_risk

    # Floor to the broker's step (round-down) so we never round up past
    # the intended risk envelope. The previous round() could bump a
    # 0.014-lot computation to 0.02 lots (43% extra risk per trade).
    stepped = (raw_lots // lot_step) * lot_step
    if stepped < min_lot:
        return 0.0
    return min(max_lot, round(stepped, 2))
