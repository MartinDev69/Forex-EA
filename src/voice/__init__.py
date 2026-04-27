"""Voice kill switch — see killswitch.py for the design."""
from src.voice.killswitch import (
    KillSwitchFlag,
    KillSwitchState,
    MatchResult,
    VoiceKillConfig,
    VoiceLogEntry,
    VoiceLogStore,
    match_phrase,
    normalize,
)

__all__ = [
    "KillSwitchFlag",
    "KillSwitchState",
    "MatchResult",
    "VoiceKillConfig",
    "VoiceLogEntry",
    "VoiceLogStore",
    "match_phrase",
    "normalize",
]
