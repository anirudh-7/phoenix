"""Event bus — one LISTEN connection, in-process fanout to many subscribers.

The SSE endpoint calls `bus.subscribe()` once per connected client. Each
subscriber gets an async iterator of events. Fanout is async-queue based:
the bus fetches the row, then puts it on every subscriber's queue.

Lifecycle: call `start()` on app startup, `stop()` on shutdown. For
FastAPI that happens in the `lifespan` context manager.

Backpressure: each subscriber's queue has a bounded size. If a slow
consumer's queue fills, we drop events for that consumer only and log a
warning — we never block the publisher. For the dashboard use case (tens
of events per second, maybe) this is fine. If we ever start missing
dashboard events we'll increase the queue size; if we still miss them
we'll rethink.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator
from uuid import UUID

import asyncpg

from phoenix.config import settings

log = logging.getLogger(__name__)

CHANNEL = "phoenix_events"
SUBSCRIBER_QUEUE_SIZE = 256


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BusEvent:
    """What subscribers receive. Mirrors the columns on `events`."""

    id: int
    kind: str
    run_id: UUID | None
    step_id: UUID | None
    action_id: UUID | None
    payload: dict[str, Any]
    created_at: datetime

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> BusEvent:
        return cls(
            id=row["id"],
            kind=row["kind"],
            run_id=row["run_id"],
            step_id=row["step_id"],
            action_id=row["action_id"],
            payload=dict(row["payload"]) if row["payload"] else {},
            created_at=row["created_at"],
        )


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


def _asyncpg_url() -> str:
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


class EventBus:
    def __init__(self) -> None:
        self._conn: asyncpg.Connection | None = None
        self._subscribers: set[asyncio.Queue[BusEvent]] = set()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if self._conn is not None:
            return
        self._conn = await asyncpg.connect(_asyncpg_url())
        await self._conn.add_listener(CHANNEL, self._on_notify)
        log.info("event bus started; listening on '%s'", CHANNEL)

    async def stop(self) -> None:
        if self._conn is None:
            return
        await self._conn.remove_listener(CHANNEL, self._on_notify)
        await self._conn.close()
        self._conn = None
        # Cancel any in-flight fanout tasks.
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        log.info("event bus stopped")

    async def subscribe(self) -> AsyncIterator[BusEvent]:
        """Yield events as they arrive. Caller cleans up by exiting the loop."""
        queue: asyncio.Queue[BusEvent] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    # -- internals ----------------------------------------------------------

    def _on_notify(
        self,
        _conn: asyncpg.Connection,
        _pid: int,
        _channel: str,
        payload: str,
    ) -> None:
        """Runs in asyncpg's reader task — must be non-blocking."""
        try:
            event_id = int(payload)
        except ValueError:
            log.warning("unparseable NOTIFY payload: %r", payload)
            return
        task = asyncio.create_task(self._fetch_and_fanout(event_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fetch_and_fanout(self, event_id: int) -> None:
        assert self._conn is not None
        row = await self._conn.fetchrow(
            "SELECT id, kind, run_id, step_id, action_id, payload, created_at "
            "FROM events WHERE id = $1",
            event_id,
        )
        if row is None:
            log.warning("NOTIFY referenced missing event id=%s", event_id)
            return
        event = BusEvent.from_row(row)

        async with self._lock:
            subscribers = list(self._subscribers)

        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("dropping event #%s for slow subscriber", event.id)


# Module-level singleton. Wired up in phoenix.api's lifespan.
bus = EventBus()
