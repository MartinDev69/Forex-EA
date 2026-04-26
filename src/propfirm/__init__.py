"""Prop-firm challenge mode.

Enforces FTMO-style rules — daily DD, total DD, lot cap, mandatory stop —
on top of the regular risk gates. Off by default; enable per challenge
account via PROPFIRM_ENABLED=1 + a preset (PROPFIRM_PRESET=ftmo, ...).
"""
from .guard import PropFirmGuard, PropFirmDecision
from .policy import PRESETS, PropFirmPolicy, policy_from_env
from .store import PropFirmState, PropFirmStore

__all__ = [
    "PRESETS",
    "PropFirmDecision",
    "PropFirmGuard",
    "PropFirmPolicy",
    "PropFirmState",
    "PropFirmStore",
    "policy_from_env",
]
