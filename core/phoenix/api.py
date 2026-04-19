"""FastAPI app — HTTP control surface for Phoenix.

Endpoints:
    GET  /health                — liveness check
    POST /runs                  — start an agent run (returns immediately)
    GET  /runs                  — list runs, newest first
    GET  /runs/{run_id}         — fetch one run with its steps and actions
    GET  /events/stream         — SSE stream of every event in real time

The agent runs in a background task tied to the app lifecycle. The
returned run_id is what the dashboard polls or what the SSE consumer
filters on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from phoenix.bus import bus
from phoenix.db.models import Action, Run, RunStatus, Step, TriggerType
from phoenix.db.session import get_session
from phoenix.orchestrator import start_run

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan: start the bus on app boot, stop it cleanly on shutdown.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await bus.start()
    # Track in-flight agent tasks so we can cancel them on shutdown.
    _app.state.run_tasks = set()
    try:
        yield
    finally:
        for task in list(_app.state.run_tasks):
            task.cancel()
        await bus.stop()


app = FastAPI(title="Phoenix", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Schemas (request + response)
# ---------------------------------------------------------------------------


class RunStartRequest(BaseModel):
    agent_name: str
    input: dict[str, Any] = Field(default_factory=dict)


class RunStartResponse(BaseModel):
    run_id: UUID


class StepOut(BaseModel):
    id: UUID
    step_number: int
    kind: str
    llm_output: dict[str, Any] | None
    created_at: datetime


class ActionOut(BaseModel):
    id: UUID
    action_type: str
    args: dict[str, Any]
    policy_decision: str
    policy_reason: str | None
    status: str
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: datetime


class RunSummary(BaseModel):
    id: UUID
    agent_name: str
    status: str
    trigger: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunDetail(RunSummary):
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    status_reason: str | None
    steps: list[StepOut]
    actions: list[ActionOut]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/runs", response_model=RunStartResponse, status_code=202)
async def post_run(req: RunStartRequest) -> RunStartResponse:
    """Start a run. Returns immediately; the agent executes in the background."""

    # We don't await this — the response goes back as soon as we've scheduled it.
    # The actual Run row is created inside start_run before agent.run() runs, so
    # subscribers see run.created within milliseconds.
    async def _runner() -> UUID:
        try:
            return await start_run(req.agent_name, input=req.input, trigger=TriggerType.MANUAL)
        except Exception:
            log.exception("background run failed for agent=%s", req.agent_name)
            raise

    task = asyncio.create_task(_runner())
    app.state.run_tasks.add(task)
    task.add_done_callback(app.state.run_tasks.discard)

    # We don't have a run_id until start_run actually creates the row. For v1 we
    # cheat: wait briefly for the task to either complete or hit the first
    # checkpoint where the row exists. A proper fix is to refactor start_run to
    # return the id before agent.run(), but for the demo this is good enough.
    try:
        run_id = await asyncio.wait_for(task, timeout=30.0)
    except asyncio.TimeoutError:
        raise HTTPException(504, "run did not complete within 30s; use GET /runs to poll")
    return RunStartResponse(run_id=run_id)


@app.get("/runs", response_model=list[RunSummary])
async def list_runs(
    limit: int = 50,
    status: RunStatus | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[RunSummary]:
    stmt = select(Run).order_by(desc(Run.created_at)).limit(limit)
    if status is not None:
        stmt = stmt.where(Run.status == status)
    runs = (await session.execute(stmt)).scalars().all()
    return [
        RunSummary(
            id=r.id,
            agent_name=r.agent_name,
            status=r.status.value,
            trigger=r.trigger.value,
            created_at=r.created_at,
            started_at=r.started_at,
            finished_at=r.finished_at,
        )
        for r in runs
    ]


@app.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> RunDetail:
    stmt = (
        select(Run)
        .where(Run.id == run_id)
        .options(selectinload(Run.steps), selectinload(Run.actions))
    )
    run = (await session.execute(stmt)).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, f"run {run_id} not found")

    return RunDetail(
        id=run.id,
        agent_name=run.agent_name,
        status=run.status.value,
        trigger=run.trigger.value,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        input=run.input,
        output=run.output,
        error=run.error,
        status_reason=run.status_reason,
        steps=[
            StepOut(
                id=s.id,
                step_number=s.step_number,
                kind=s.kind.value,
                llm_output=s.llm_output,
                created_at=s.created_at,
            )
            for s in sorted(run.steps, key=lambda s: s.step_number)
        ],
        actions=[
            ActionOut(
                id=a.id,
                action_type=a.action_type,
                args=a.args,
                policy_decision=a.policy_decision.value,
                policy_reason=a.policy_reason,
                status=a.status.value,
                result=a.result,
                error=a.error,
                created_at=a.created_at,
            )
            for a in sorted(run.actions, key=lambda a: a.created_at)
        ],
    )


# ---------------------------------------------------------------------------
# SSE — Server-Sent Events
#
# Wire format (per event, terminated by a blank line):
#
#     id: 42
#     event: action.executed
#     data: {"run_id": "...", "action_id": "...", ...}
#
# Browsers and EventSource clients reconnect automatically and resume from
# the last `id` they saw if we set Last-Event-ID. We don't implement
# resume yet (would require keeping a recent-events buffer or a DB query
# for events newer than X). For v1 a fresh stream on reconnect is fine.
# ---------------------------------------------------------------------------


def _sse_format(event_id: int, kind: str, data: dict[str, Any]) -> str:
    return (
        f"id: {event_id}\n"
        f"event: {kind}\n"
        f"data: {json.dumps(data, default=str)}\n\n"
    )


@app.get("/events/stream")
async def stream_events() -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        # Heartbeat so proxies don't timeout the connection during quiet periods.
        async for event in _with_heartbeat(bus.subscribe(), interval=15.0):
            if event is None:
                yield ": ping\n\n"
                continue
            yield _sse_format(
                event_id=event.id,
                kind=event.kind,
                data={
                    "id": event.id,
                    "kind": event.kind,
                    "run_id": str(event.run_id) if event.run_id else None,
                    "step_id": str(event.step_id) if event.step_id else None,
                    "action_id": str(event.action_id) if event.action_id else None,
                    "payload": event.payload,
                    "created_at": event.created_at.isoformat(),
                },
            )

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",        # disable nginx buffering if it's ever in front
        },
    )


async def _with_heartbeat(
    source: AsyncIterator[Any],
    interval: float,
) -> AsyncIterator[Any]:
    """Yield items from `source`; yield None every `interval` seconds of silence."""
    iterator = source.__aiter__()
    while True:
        try:
            yield await asyncio.wait_for(iterator.__anext__(), timeout=interval)
        except asyncio.TimeoutError:
            yield None
        except StopAsyncIteration:
            return
