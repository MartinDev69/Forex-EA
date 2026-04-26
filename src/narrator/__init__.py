"""Post-trade LLM narrator.

Composes a short paragraph explaining what the bot saw, what it did, and
how the trade resolved — using the same context the operator would read
from /trade/{id}/explanation, plus the close outcome. Off by default;
opt-in per account so personal accounts and low-volume challenges can
skip the API spend.
"""
from .composer import NarratorComposer
from .provider import (
    AnthropicProvider,
    LLMProvider,
    OpenAIProvider,
    StubProvider,
    build_provider,
)
from .store import NarrativeStore, TradeNarrative

__all__ = [
    "AnthropicProvider",
    "LLMProvider",
    "NarrativeStore",
    "NarratorComposer",
    "OpenAIProvider",
    "StubProvider",
    "TradeNarrative",
    "build_provider",
]
