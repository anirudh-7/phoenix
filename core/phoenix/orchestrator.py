"""Run orchestration — the spine.

`start_run()` is the entry point. Given an agent name and inputs, it:

  1. Creates a Run row (status=QUEUED, then RUNNING).
  2. Builds a RunContext for the agent to use.
  3. Calls agent.run(ctx) and waits for it to complete.
  4. Writes the final Run state (DONE / FAILED / AWAITING_APPROVAL).

The agent interacts with the outside world *only* through `RunContext`:

    await ctx.think("a thought")              # logs a Step of kind=think
    result = await ctx.propose(action, args)  # policy gate + execute or queue

Every state change writes a row to `events` so consumers (the dashboard,
audit, reload-after-crash) can reconstruct what happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from phoenix import tools
from phoenix.db.models import (
    Action,
    ActionStatus,
    Event,
    PolicyDecision,
    Run,
    RunStatus,
    Step,
    StepKind,
    TriggerType,
)
from phoenix.db.session import session_factory
from phoenix.events import EventKind
from phoenix.policy import Policy

if TYPE_CHECKING:
    from phoenix.agents import Agent


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalResult:
    """Returned to the agent from ctx.propose()."""

    action_id: UUID
    status: ActionStatus       # SUCCEEDED | AWAITING_APPROVAL | REJECTED | FAILED
    decision: PolicyDecision   # what the policy said
    reason: str                # why the policy said it
    result: dict[str, Any] | None = None   # populated if status=SUCCEEDED
    error: str | None = None               # populated if status=FAILED|REJECTED


@dataclass
class RunResult:
    """Returned by agent.run() to indicate clean completion."""

    output: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# RunContext — the agent's window into Phoenix.
# ---------------------------------------------------------------------------


class RunContext:
    """Per-run handle. Wraps a DB session and a step counter."""

    def __init__(
        self,
        run: Run,
        session: AsyncSession,
        policy: Policy,
        agent_name: str,
    ) -> None:
        self._run = run
        self._session = session
        self._policy = policy
        self._agent_name = agent_name
        self._step_number = 0

    @property
    def run_id(self) -> UUID:
        return self._run.id

    @property
    def input(self) -> dict[str, Any]:
        return self._run.input

    async def think(self, message: str) -> Step:
        """Record a 'think' step. The agent's narration of what it's doing."""
        return await self._record_step(
            kind=StepKind.THINK,
            llm_output={"message": message},
        )

    async def propose(
        self,
        action_type: str,
        args: dict[str, Any] | None = None,
    ) -> ProposalResult:
        """Propose an action. Policy decides; orchestrator executes or queues."""
        args = args or {}

        step = await self._record_step(
            kind=StepKind.TOOL_CALL,
            llm_output={"action_type": action_type, "args": args},
        )

        decision = self._policy.evaluate(self._agent_name, action_type, args)

        action = Action(
            run_id=self._run.id,
            step_id=step.id,
            agent_name=self._agent_name,
            action_type=action_type,
            args=args,
            policy_decision=decision.decision,
            policy_reason=decision.reason,
            status=ActionStatus.PENDING,
        )
        self._session.add(action)
        await self._session.flush()
        await self._emit(EventKind.ACTION_PROPOSED, action_id=action.id, step_id=step.id)

        # Route by decision.
        if decision.decision is PolicyDecision.BLOCKED:
            action.status = ActionStatus.REJECTED
            action.error = {"reason": decision.reason}
            await self._session.flush()
            await self._emit(EventKind.ACTION_BLOCKED, action_id=action.id)
            return ProposalResult(
                action_id=action.id,
                status=ActionStatus.REJECTED,
                decision=decision.decision,
                reason=decision.reason,
                error=decision.reason,
            )

        if decision.decision is PolicyDecision.APPROVAL:
            action.status = ActionStatus.AWAITING_APPROVAL
            await self._session.flush()
            await self._emit(EventKind.ACTION_PENDING_APPROVAL, action_id=action.id)
            return ProposalResult(
                action_id=action.id,
                status=ActionStatus.AWAITING_APPROVAL,
                decision=decision.decision,
                reason=decision.reason,
            )

        # AUTO — execute now.
        return await self._execute(action)

    # -- internals ----------------------------------------------------------

    async def _execute(self, action: Action) -> ProposalResult:
        tool = tools.get(action.action_type)
        if tool is None:
            action.status = ActionStatus.FAILED
            action.error = {"reason": f"no tool registered for {action.action_type}"}
            await self._session.flush()
            await self._emit(EventKind.ACTION_FAILED, action_id=action.id)
            return ProposalResult(
                action_id=action.id,
                status=ActionStatus.FAILED,
                decision=action.policy_decision,
                reason=action.policy_reason or "",
                error=f"no tool registered for {action.action_type}",
            )

        action.status = ActionStatus.EXECUTING
        await self._session.flush()

        try:
            result = await tool(action.args)
        except Exception as exc:                            # noqa: BLE001 — last line of defence
            action.status = ActionStatus.FAILED
            action.error = {"type": type(exc).__name__, "message": str(exc)}
            await self._session.flush()
            await self._emit(EventKind.ACTION_FAILED, action_id=action.id)
            return ProposalResult(
                action_id=action.id,
                status=ActionStatus.FAILED,
                decision=action.policy_decision,
                reason=action.policy_reason or "",
                error=str(exc),
            )

        action.status = ActionStatus.SUCCEEDED
        action.result = result
        await self._session.flush()
        await self._emit(EventKind.ACTION_EXECUTED, action_id=action.id)

        return ProposalResult(
            action_id=action.id,
            status=ActionStatus.SUCCEEDED,
            decision=action.policy_decision,
            reason=action.policy_reason or "",
            result=result,
        )

    async def _record_step(
        self,
        kind: StepKind,
        llm_output: dict[str, Any] | None = None,
    ) -> Step:
        self._step_number += 1
        step = Step(
            run_id=self._run.id,
            step_number=self._step_number,
            kind=kind,
            llm_output=llm_output,
        )
        self._session.add(step)
        await self._session.flush()
        await self._emit(EventKind.STEP_STARTED, step_id=step.id)
        await self._emit(EventKind.STEP_COMPLETED, step_id=step.id)
        return step

    async def _emit(
        self,
        kind: EventKind,
        *,
        step_id: UUID | None = None,
        action_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        evt = Event(
            kind=kind.value,
            run_id=self._run.id,
            step_id=step_id,
            action_id=action_id,
            payload=payload or {},
        )
        self._session.add(evt)
        await self._session.flush()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def start_run(
    agent_name: str,
    *,
    input: dict[str, Any] | None = None,
    trigger: TriggerType = TriggerType.MANUAL,
    trigger_context: dict[str, Any] | None = None,
    policy: Policy | None = None,
) -> UUID:
    """Run an agent end-to-end. Returns the run_id.

    Blocks until the agent finishes. For v1 this is fine — the FastAPI
    layer (Turn 3c) will wrap this in a background task so HTTP requests
    return quickly.
    """
    from phoenix.agents import get_agent

    pol = policy or Policy.load()
    agent = get_agent(agent_name)

    async with session_factory() as session:
        async with session.begin():
            run = Run(
                agent_name=agent_name,
                trigger=trigger,
                trigger_context=trigger_context or {},
                input=input or {},
                status=RunStatus.QUEUED,
            )
            session.add(run)
            await session.flush()
            await _emit_run_event(session, run, EventKind.RUN_CREATED)

            run.status = RunStatus.RUNNING
            run.started_at = _now()
            await session.flush()
            await _emit_run_event(session, run, EventKind.RUN_STARTED)

            ctx = RunContext(run=run, session=session, policy=pol, agent_name=agent_name)

            try:
                result = await agent.run(ctx)
            except Exception as exc:                        # noqa: BLE001
                run.status = RunStatus.FAILED
                run.error = {"type": type(exc).__name__, "message": str(exc)}
                run.finished_at = _now()
                await session.flush()
                await _emit_run_event(session, run, EventKind.RUN_FAILED)
                raise

            # Decide final state by inspecting the actions we touched.
            has_pending = await _run_has_pending_approvals(session, run.id)
            if has_pending:
                run.status = RunStatus.AWAITING_APPROVAL
                await session.flush()
                await _emit_run_event(session, run, EventKind.RUN_AWAITING_APPROVAL)
            else:
                run.status = RunStatus.DONE
                run.output = result.output
                run.finished_at = _now()
                await session.flush()
                await _emit_run_event(session, run, EventKind.RUN_COMPLETED)

            return run.id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


async def _emit_run_event(session: AsyncSession, run: Run, kind: EventKind) -> None:
    session.add(Event(kind=kind.value, run_id=run.id))
    await session.flush()


async def _run_has_pending_approvals(session: AsyncSession, run_id: UUID) -> bool:
    from sqlalchemy import select
    stmt = (
        select(Action.id)
        .where(Action.run_id == run_id, Action.status == ActionStatus.AWAITING_APPROVAL)
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None
