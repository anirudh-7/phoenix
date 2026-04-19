"""End-to-end smoketest for the foundation layer.

Proves that:
  1. Settings load from .env
  2. The async engine connects to Postgres
  3. The ORM models match the schema (insert + select round-trip)
  4. Postgres ENUMs are wired correctly (we write a RunStatus, read it back)

Run with:
    uv run python -m scripts.smoketest

Expected output: a few lines ending in "OK: foundation verified".
Exits non-zero if anything is wrong.
"""

from __future__ import annotations

import asyncio
import sys
from uuid import uuid4

from sqlalchemy import select

from phoenix.config import settings
from phoenix.db import session_factory
from phoenix.db.models import Agent, Run, RunStatus, TriggerType


async def main() -> None:
    print(f"database_url = {settings.database_url}")

    async with session_factory() as session:
        # 1. Insert an Agent. The agents table has no ENUM columns — this
        #    checks plain text PK + JSONB + server-side timestamps.
        agent_name = f"smoketest-{uuid4().hex[:8]}"
        agent = Agent(
            name=agent_name,
            version="0.0.0",
            description="Ephemeral agent inserted by smoketest.py",
            config={"purpose": "verify foundation"},
        )
        session.add(agent)
        await session.flush()     # sends INSERT, populates server defaults
        print(f"inserted agent  name={agent.name}  enabled={agent.enabled}")

        # 2. Insert a Run using the two ENUM columns (status + trigger).
        #    If pg_enum / values_callable are wrong this explodes.
        run = Run(
            agent_name=agent.name,
            status=RunStatus.QUEUED,
            trigger=TriggerType.MANUAL,
            input={"note": "hello from smoketest"},
        )
        session.add(run)
        await session.flush()
        print(f"inserted run    id={run.id}  status={run.status}  trigger={run.trigger}")

        # 3. Read it back through the ORM.
        stmt = select(Run).where(Run.id == run.id)
        fetched = (await session.execute(stmt)).scalar_one()
        assert fetched.status is RunStatus.QUEUED, f"enum round-trip failed: {fetched.status!r}"
        assert fetched.input == {"note": "hello from smoketest"}, "JSONB round-trip failed"
        print(f"fetched run     status={fetched.status}  input={fetched.input}")

        # 4. Roll it all back — smoketest should leave the DB untouched.
        await session.rollback()
        print("rolled back     (no rows persisted)")

    print("OK: foundation verified")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
