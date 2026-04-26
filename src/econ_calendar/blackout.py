"""Per-signal blackout decision.

A signal on `symbol` at `now` is blocked iff there is an event E such that
  E.currency ∈ currencies_for_symbol(symbol)
  E.impact   ∈ policy.impacts
  E.event_time ∈ (now - after_min, now + before_min)

Interpretation: the blackout window is [E.event_time - before, E.event_time + after];
equivalently, at time `now` the set of events inside their own blackout windows is
`event_time ∈ (now - after, now + before)`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .store import CalendarEvent, EventStore
from .symbols import currencies_for_symbol


@dataclass
class BlackoutPolicy:
    enabled: bool = True
    before_min: int = 15
    after_min: int = 15
    impacts: frozenset[str] = field(default_factory=lambda: frozenset({"high"}))

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BlackoutPolicy":
        import os
        e = env if env is not None else os.environ
        impacts_raw = e.get("CALENDAR_BLACKOUT_IMPACTS", "high")
        impacts = frozenset(i.strip().lower() for i in impacts_raw.split(",") if i.strip())
        return cls(
            enabled=e.get("CALENDAR_BLACKOUT_ENABLED", "1").strip() not in ("0", "false", "False", ""),
            before_min=int(e.get("CALENDAR_BLACKOUT_BEFORE_MIN", "15")),
            after_min=int(e.get("CALENDAR_BLACKOUT_AFTER_MIN", "15")),
            impacts=impacts or frozenset({"high"}),
        )


class BlackoutChecker:
    def __init__(self, store: EventStore, policy: BlackoutPolicy | None = None) -> None:
        self.store = store
        self.policy = policy or BlackoutPolicy()

    def current_blackout(
        self,
        symbol: str,
        now: datetime | None = None,
    ) -> CalendarEvent | None:
        """Return the event blocking trading on `symbol` right now, or None."""
        if not self.policy.enabled:
            return None
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        ccys = currencies_for_symbol(symbol)
        if not ccys:
            return None
        window_start = now - timedelta(minutes=self.policy.after_min)
        window_end = now + timedelta(minutes=self.policy.before_min)
        events = self.store.events_in_window(ccys, window_start, window_end, self.policy.impacts)
        return events[0] if events else None

    def next_event(
        self,
        symbol: str,
        now: datetime | None = None,
    ) -> CalendarEvent | None:
        """Return the next upcoming event that would affect `symbol`."""
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        ccys = currencies_for_symbol(symbol)
        if not ccys:
            return None
        return self.store.next_event(ccys, now, self.policy.impacts)

    def status(self, symbol: str, now: datetime | None = None) -> dict:
        """Dashboard-friendly snapshot: blackout flag + current + next event."""
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        current = self.current_blackout(symbol, now)
        nxt = self.next_event(symbol, now)
        return {
            "symbol": symbol,
            "blackout": current is not None,
            "enabled": self.policy.enabled,
            "before_min": self.policy.before_min,
            "after_min": self.policy.after_min,
            "current_event": current.to_dict() if current else None,
            "next_event": nxt.to_dict() if nxt else None,
            "minutes_until_next": (nxt.minutes_until(now) if nxt else None),
        }
