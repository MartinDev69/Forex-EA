"""Periodic refresh of the calendar cache.

Runs on the FastAPI event loop: a single asyncio task that sleeps
`interval_s` between refreshes and catches every exception so a flaky
upstream never kills the loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .provider import CalendarProvider
from .store import EventStore

log = logging.getLogger(__name__)


def run_once(provider: CalendarProvider, store: EventStore, purge_older_than_days: int = 7) -> int:
    """Fetch + persist. Returns number of events upserted. Safe to call from a thread."""
    events = provider.fetch()
    wrote = store.upsert_many(events)
    if purge_older_than_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=purge_older_than_days)
        store.purge_before(cutoff)
    log.info("calendar refresh: upserted %d events, total=%d", wrote, store.count())
    return wrote


class CalendarRefresher:
    """Background asyncio task driving repeated refreshes."""

    def __init__(
        self,
        provider: CalendarProvider,
        store: EventStore,
        interval_s: int = 1800,
    ) -> None:
        self.provider = provider
        self.store = store
        self.interval_s = interval_s
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _loop(self) -> None:
        # Initial refresh on startup — blocking the first cycle is fine because
        # it runs inside the asyncio task, not in the request path.
        await self._tick()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                await self._tick()

    async def _tick(self) -> None:
        try:
            await asyncio.to_thread(run_once, self.provider, self.store)
        except Exception:
            log.exception("calendar refresh failed — will retry next cycle")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="calendar-refresher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
