"""Replay-with-different-params.

Captures the OHLC bars over a trade's lifecycle at close time and
replays them through a tweaked SL/TP to answer 'what if I had moved my
stop a few pips wider?' Operator-facing diagnostic — not in the trading
hot path beyond a single small INSERT batch per close.
"""
from .engine import ReplayEngine, ReplayRequest, ReplayResult
from .path_store import PathBar, PathStore
from .recorder import PathRecorder

__all__ = [
    "PathBar",
    "PathRecorder",
    "PathStore",
    "ReplayEngine",
    "ReplayRequest",
    "ReplayResult",
]
