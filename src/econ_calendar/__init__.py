"""Economic-calendar blackout.

Prevents the bot from trading in a ±N-minute window around high-impact macro
events (NFP, CPI, FOMC, ECB rate decision, …). Spiky news candles routinely
stop-out trades that never should have fired, so the cleanest fix is to
simply refuse signals during the blackout window.

Architecture:
  providers/   fetch upcoming events (ForexFactory public JSON by default)
  store        persist events in SQLite; indexed by (currency, event_time)
  blackout     maps (symbol, now) → Event|None using a BlackoutPolicy
  refresher    periodic background fetch on the FastAPI event loop

The RiskManager holds an optional BlackoutChecker and rejects with
`reason="calendar:USD:Non-Farm Payrolls"` so the trade journal keeps a
meaningful audit trail of *why* each signal was dropped.
"""
from .blackout import BlackoutChecker, BlackoutPolicy
from .provider import CalendarProvider, ForexFactoryProvider, StaticProvider
from .store import CalendarEvent, EventStore
from .symbols import currencies_for_symbol

__all__ = [
    "BlackoutChecker",
    "BlackoutPolicy",
    "CalendarEvent",
    "CalendarProvider",
    "EventStore",
    "ForexFactoryProvider",
    "StaticProvider",
    "currencies_for_symbol",
]
