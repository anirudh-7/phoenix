"""Live event tail.

Listens on the `phoenix_events` channel and prints every event as it's
written. Open this in one terminal, run `scripts/run_demo.py` in another,
and you'll see the events scroll past in real time.

Usage:
    uv run python -m scripts.tail_events
    Ctrl-C to quit.
"""

from __future__ import annotations

import asyncio
import json
import signal
from typing import Any

import asyncpg

from phoenix.config import settings


CHANNEL = "phoenix_events"


def asyncpg_url() -> str:
    """asyncpg expects 'postgresql://', not 'postgresql+asyncpg://'."""
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


def format_event(row: dict[str, Any]) -> str:
    payload_repr = ""
    if row.get("payload"):
        payload_repr = f"  payload={json.dumps(row['payload'])}"
    refs = []
    if row.get("run_id"):
        refs.append(f"run={str(row['run_id'])[:8]}")
    if row.get("step_id"):
        refs.append(f"step={str(row['step_id'])[:8]}")
    if row.get("action_id"):
        refs.append(f"action={str(row['action_id'])[:8]}")
    refs_repr = "  " + " ".join(refs) if refs else ""
    return f"[event #{row['id']:>4}] {row['kind']:<28s}{refs_repr}{payload_repr}"


async def main() -> None:
    conn = await asyncpg.connect(asyncpg_url())
    print(f"listening on '{CHANNEL}' (Ctrl-C to quit)")

    queue: asyncio.Queue[int] = asyncio.Queue()

    def on_notify(_conn: object, _pid: int, _channel: str, payload: str) -> None:
        # Callback runs in asyncpg's reader task — keep it tiny. Just push
        # the event id onto the queue; the consumer below does the SELECT.
        try:
            queue.put_nowait(int(payload))
        except ValueError:
            print(f"!! unparseable payload: {payload!r}")

    await conn.add_listener(CHANNEL, on_notify)

    # Graceful shutdown on Ctrl-C.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        while not stop.is_set():
            try:
                event_id = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            row = await conn.fetchrow(
                "SELECT id, kind, run_id, step_id, action_id, payload "
                "FROM events WHERE id = $1",
                event_id,
            )
            if row is None:
                print(f"!! event {event_id} not found (deleted?)")
                continue
            print(format_event(dict(row)))
    finally:
        await conn.remove_listener(CHANNEL, on_notify)
        await conn.close()
        print("\nclosed.")


if __name__ == "__main__":
    asyncio.run(main())
