"""End-to-end spine test.

Runs the DemoAgent and prints everything that happened in the DB so you
can see the orchestrator, policy gate, tool executor, and event writer
all cooperating.

Usage:
    uv run python -m scripts.run_demo
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from phoenix.db.models import Action, Agent, Event, Run, Step
from phoenix.db.session import session_factory
from phoenix.orchestrator import start_run


async def ensure_demo_agent_row() -> None:
    """The runs.agent_name column is a FK to agents.name — insert if missing."""
    async with session_factory() as session:
        async with session.begin():
            existing = await session.get(Agent, "demo")
            if existing is None:
                session.add(
                    Agent(
                        name="demo",
                        version="0.0.1",
                        description="No-op agent used by run_demo.py.",
                    )
                )


async def print_run_summary(run_id) -> None:
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        assert run is not None

        steps = (await session.execute(
            select(Step).where(Step.run_id == run_id).order_by(Step.step_number)
        )).scalars().all()

        actions = (await session.execute(
            select(Action).where(Action.run_id == run_id).order_by(Action.created_at)
        )).scalars().all()

        events = (await session.execute(
            select(Event).where(Event.run_id == run_id).order_by(Event.id)
        )).scalars().all()

        print()
        print(f"Run {run.id}")
        print(f"  agent        = {run.agent_name}")
        print(f"  status       = {run.status.value}")
        print(f"  started_at   = {run.started_at}")
        print(f"  finished_at  = {run.finished_at}")
        print(f"  output       = {run.output}")

        print(f"\nSteps ({len(steps)}):")
        for s in steps:
            print(f"  #{s.step_number}  {s.kind.value:12s}  {s.llm_output}")

        print(f"\nActions ({len(actions)}):")
        for a in actions:
            print(
                f"  {a.action_type:18s}  "
                f"decision={a.policy_decision.value:<8s} "
                f"status={a.status.value:<10s} "
                f"result={a.result}"
            )

        print(f"\nEvents ({len(events)}):")
        for e in events:
            print(f"  id={e.id:<4d}  {e.kind}")


async def main() -> None:
    await ensure_demo_agent_row()
    run_id = await start_run("demo", input={"note": "hello"})
    await print_run_summary(run_id)


if __name__ == "__main__":
    asyncio.run(main())
