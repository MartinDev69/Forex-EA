"""Pluggable providers that produce CalendarEvent lists.

We deliberately depend on stdlib only: urllib.request for the HTTP fetch,
json for parsing, datetime for time handling. That keeps the blast radius
tiny and avoids pulling `requests` just for one GET.

Providers:
  StaticProvider       — returns a fixed list; used in tests and for manual
                         seeding from a JSON snapshot in dev.
  ForexFactoryProvider — public weekly JSON feed hosted by Faireconomy. No
                         API key required. Mirrors the ForexFactory website;
                         used by thousands of bots, stable for years.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Protocol

from .store import CalendarEvent

log = logging.getLogger(__name__)

_FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_FF_USER_AGENT = "AntiGreed/1.0 (+calendar-blackout)"


class CalendarProvider(Protocol):
    """Anything with `.fetch()` returning a list of events."""

    def fetch(self) -> list[CalendarEvent]: ...


class StaticProvider:
    """Returns a pre-built list; perfect for tests and seeding."""

    def __init__(self, events: list[CalendarEvent]) -> None:
        self._events = list(events)

    def fetch(self) -> list[CalendarEvent]:
        return list(self._events)


class ForexFactoryProvider:
    """ForexFactory weekly JSON fetched from Faireconomy's public mirror."""

    def __init__(
        self,
        url: str = _FF_URL,
        timeout_s: float = 15.0,
        user_agent: str = _FF_USER_AGENT,
    ) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self.user_agent = user_agent

    def fetch(self) -> list[CalendarEvent]:
        raw = self._http_get_json()
        return parse_forexfactory_payload(raw)

    def _http_get_json(self):
        req = urllib.request.Request(self.url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = resp.read()
        except urllib.error.URLError as e:
            raise RuntimeError(f"calendar fetch failed: {e}") from e
        return json.loads(body.decode("utf-8"))


def parse_forexfactory_payload(payload) -> list[CalendarEvent]:
    """Parse the ForexFactory weekly JSON into CalendarEvent objects.

    Tolerates missing/null fields. Drops rows we can't understand (bad date,
    unknown impact) rather than raising — one malformed row shouldn't take
    down the whole refresh.
    """
    if not isinstance(payload, list):
        raise ValueError("ForexFactory payload must be a JSON list")

    out: list[CalendarEvent] = []
    for row in payload:
        try:
            event = _row_to_event(row)
        except Exception as exc:
            log.debug("skipping malformed calendar row: %s — row=%r", exc, row)
            continue
        if event is not None:
            out.append(event)
    return out


def _row_to_event(row: dict) -> CalendarEvent | None:
    currency = (row.get("country") or row.get("currency") or "").strip().upper()
    if not currency:
        return None

    impact_raw = (row.get("impact") or "").strip().lower()
    # ForexFactory uses "High/Medium/Low"; some mirrors use "Holiday" or "None".
    if impact_raw not in ("high", "medium", "low"):
        return None

    title = (row.get("title") or row.get("event") or "").strip()
    if not title:
        return None

    date_raw = row.get("date") or row.get("timestamp")
    event_time = _parse_datetime(date_raw)
    if event_time is None:
        return None

    return CalendarEvent(
        event_time=event_time,
        currency=currency,
        impact=impact_raw,
        title=title,
        actual=_clean(row.get("actual")),
        forecast=_clean(row.get("forecast")),
        previous=_clean(row.get("previous")),
        source="forexfactory",
    )


def _parse_datetime(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # Unix seconds — mirror format used by some variants.
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    # ForexFactory uses e.g. "2026-05-02T08:30:00-04:00" or "...Z".
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Conservatively treat naive timestamps as UTC so we don't drift.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
