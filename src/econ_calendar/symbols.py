"""Symbol → affected-currencies mapping.

Most FX pairs decompose cleanly: "EURUSD" → {EUR, USD}. Metals, indices, and
crypto need explicit entries because their tickers don't embed a currency
code. When a symbol isn't recognised we conservatively return an empty set —
the blackout checker treats that as "no known events" rather than "affected
by everything", so an unknown ticker never trades-through a news window we
*could* have filtered, but also never gets spuriously blocked by an event
with no real exposure.
"""
from __future__ import annotations

from typing import Final

_FX_CURRENCIES: Final[frozenset[str]] = frozenset({
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
    "CNY", "CNH", "SGD", "HKD", "NOK", "SEK", "MXN", "ZAR",
    "TRY", "PLN", "DKK", "CZK", "HUF", "RUB",
})

# Metals, indices, and crypto — the left side is the ticker as MT5 / most
# brokers present it; the right side is the set of currencies whose calendar
# events materially move the instrument.
_EXPLICIT: Final[dict[str, frozenset[str]]] = {
    # Precious metals priced in USD → primarily USD news + broader risk-off
    # flows that tend to follow USD/EUR prints. Keep it narrow (USD) for v1.
    "XAUUSD": frozenset({"USD"}),
    "XAGUSD": frozenset({"USD"}),
    "XPTUSD": frozenset({"USD"}),
    "XPDUSD": frozenset({"USD"}),
    # US indices
    "US30":   frozenset({"USD"}),
    "US500":  frozenset({"USD"}),
    "SPX500": frozenset({"USD"}),
    "NAS100": frozenset({"USD"}),
    "US100":  frozenset({"USD"}),
    "USTEC":  frozenset({"USD"}),
    # European indices
    "DE40":   frozenset({"EUR"}),
    "DE30":   frozenset({"EUR"}),
    "GER40":  frozenset({"EUR"}),
    "UK100":  frozenset({"GBP"}),
    "FRA40":  frozenset({"EUR"}),
    "EU50":   frozenset({"EUR"}),
    "ESP35":  frozenset({"EUR"}),
    # Asia-Pacific
    "JP225":  frozenset({"JPY"}),
    "HK50":   frozenset({"HKD", "CNY"}),
    "AUS200": frozenset({"AUD"}),
    # Commodities
    "USOIL":  frozenset({"USD"}),
    "UKOIL":  frozenset({"USD", "GBP"}),
    "WTI":    frozenset({"USD"}),
    "BRENT":  frozenset({"USD", "GBP"}),
    # Crypto — tracked against USD liquidity conditions
    "BTCUSD": frozenset({"USD"}),
    "ETHUSD": frozenset({"USD"}),
    "XRPUSD": frozenset({"USD"}),
    "LTCUSD": frozenset({"USD"}),
}


def currencies_for_symbol(symbol: str) -> frozenset[str]:
    """Return the set of ISO currencies whose events affect `symbol`.

    Returns empty set for unrecognised tickers. Callers treat that as "no
    blackout applies" rather than failing loudly — the bot should still trade
    exotic tickers even if we haven't classified them yet.
    """
    if not symbol:
        return frozenset()
    s = symbol.upper().replace("/", "").replace("_", "").replace("-", "")
    # Strip common broker suffixes like "EURUSD.m", "EURUSDpro", "EURUSD-ECN".
    # We already stripped '-'; also strip trailing ".m", ".pro", "m", etc.
    for suffix in (".M", ".PRO", ".ECN", ".RAW", ".I", ".C"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    # Trailing single-char suffix (Exness "EURUSDm") if what remains is > 6.
    if len(s) > 6 and s[-1].isalpha() and s[:-1][-3:] in _FX_CURRENCIES | {"USD"}:
        s = s[:-1]

    if s in _EXPLICIT:
        return _EXPLICIT[s]

    # FX pair: split on the first known currency code (base), then the rest
    # is quote. Handles 6-char (EURUSD) and 7-char variants cleanly.
    if len(s) >= 6:
        base = s[:3]
        quote = s[3:6]
        if base in _FX_CURRENCIES and quote in _FX_CURRENCIES:
            return frozenset({base, quote})

    return frozenset()
