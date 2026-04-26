"""Pluggable LLM providers for the post-trade narrator.

Three implementations:
  * StubProvider — deterministic template, no network call. Used in tests
    and as the default when no API key is configured.
  * AnthropicProvider — calls Anthropic's Messages API.
  * OpenAIProvider — calls OpenAI's Chat Completions API.

Both real providers use stdlib http.client so we don't pull a new
dependency just for two POSTs. Failures bubble up as RuntimeError; the
caller (NarratorComposer) catches and logs but doesn't crash the bot.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None


class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, user: str, max_tokens: int = 350) -> LLMResponse: ...


class StubProvider:
    """Zero-network deterministic provider — composes a one-liner from the
    user prompt's structure. Useful for tests and as the fallback when no
    API key is present.
    """

    name = "stub"

    def complete(self, system: str, user: str, max_tokens: int = 350) -> LLMResponse:
        # Cheap heuristic: pull the headline numbers the composer always puts on
        # the first lines of the user prompt, then echo them back as a sentence.
        lines = [line.strip() for line in user.splitlines() if line.strip()]
        head = " | ".join(lines[:6])
        return LLMResponse(
            text=f"[stub narrator] {head}",
            prompt_tokens=len(user) // 4,
            output_tokens=len(head) // 4,
            model="stub",
        )


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5-20251001",
        timeout_s: float = 20.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s

    def complete(self, system: str, user: str, max_tokens: int = 350) -> LLMResponse:
        import http.client
        body = json.dumps({
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        })
        conn = http.client.HTTPSConnection("api.anthropic.com", timeout=self.timeout_s)
        try:
            conn.request(
                "POST", "/v1/messages", body=body,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8")
            if resp.status >= 400:
                raise RuntimeError(f"anthropic {resp.status}: {raw[:200]}")
            payload = json.loads(raw)
        finally:
            conn.close()
        text = "".join(
            block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text"
        ).strip()
        usage = payload.get("usage") or {}
        return LLMResponse(
            text=text,
            prompt_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            model=payload.get("model", self.model),
        )


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        timeout_s: float = 20.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s

    def complete(self, system: str, user: str, max_tokens: int = 350) -> LLMResponse:
        import http.client
        body = json.dumps({
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        })
        conn = http.client.HTTPSConnection("api.openai.com", timeout=self.timeout_s)
        try:
            conn.request(
                "POST", "/v1/chat/completions", body=body,
                headers={
                    "authorization": f"Bearer {self.api_key}",
                    "content-type": "application/json",
                },
            )
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8")
            if resp.status >= 400:
                raise RuntimeError(f"openai {resp.status}: {raw[:200]}")
            payload = json.loads(raw)
        finally:
            conn.close()
        text = (
            payload.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        usage = payload.get("usage") or {}
        return LLMResponse(
            text=text,
            prompt_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            model=payload.get("model", self.model),
        )


def build_provider(env: dict | None = None) -> LLMProvider:
    """Pick a provider from env.

    NARRATOR_PROVIDER controls which one. Falls back to stub when the chosen
    provider has no API key — silent degradation is safer than crashing the
    bot's close path.
    """
    e = env if env is not None else os.environ
    name = (e.get("NARRATOR_PROVIDER") or "stub").strip().lower()
    if name == "anthropic":
        key = e.get("NARRATOR_API_KEY") or e.get("ANTHROPIC_API_KEY") or ""
        if not key:
            log.warning("NARRATOR_PROVIDER=anthropic but no API key — using stub")
            return StubProvider()
        return AnthropicProvider(
            api_key=key,
            model=e.get("NARRATOR_MODEL") or "claude-haiku-4-5-20251001",
        )
    if name == "openai":
        key = e.get("NARRATOR_API_KEY") or e.get("OPENAI_API_KEY") or ""
        if not key:
            log.warning("NARRATOR_PROVIDER=openai but no API key — using stub")
            return StubProvider()
        return OpenAIProvider(
            api_key=key,
            model=e.get("NARRATOR_MODEL") or "gpt-4o-mini",
        )
    return StubProvider()
