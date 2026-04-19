"""Agent contracts.

An agent is a coroutine that uses a `RunContext` to interact with the
outside world. It never touches the DB or the policy gate directly —
every proposal goes through the context, which is how the orchestrator
enforces safety and observability.

The Protocol here is intentionally tiny. Real agents (Mercury, Mars) will
implement more state on the class, but the `run(ctx)` coroutine is the
only thing the orchestrator needs.

For v1 we keep every agent in this single module. When the list grows,
promote to a package: agents/{mercury,mars,...}.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from phoenix.orchestrator import RunContext, RunResult


@runtime_checkable
class Agent(Protocol):
    """Minimal contract every Phoenix agent implements."""

    name: str                          # matches agents.name column

    async def run(self, ctx: RunContext) -> RunResult: ...


# ---------------------------------------------------------------------------
# DemoAgent — used by the end-to-end smoketest.
#
# It doesn't talk to Gmail, Kite, or any LLM. Its only job is to prove
# the spine: think → propose → policy check → execute → complete.
# ---------------------------------------------------------------------------


class DemoAgent:
    """A no-op agent that drives one notify.push action through the spine."""

    name = "demo"

    async def run(self, ctx):
        from phoenix.orchestrator import RunResult  # local import avoids cycle

        await ctx.think("Demo agent starting up.")
        result = await ctx.propose(
            action_type="notify.push",
            args={"title": "Phoenix", "message": "demo agent completed successfully"},
        )
        await ctx.think(f"notify.push returned status={result.status.value}")
        return RunResult(output={"action_status": result.status.value})


# ---------------------------------------------------------------------------
# Registry
#
# For v1 we map agent name → factory at import time. Later this could be
# a plugin system; for now a dict is fine.
# ---------------------------------------------------------------------------


_AGENTS: dict[str, type] = {
    "demo": DemoAgent,
}


def get_agent(name: str) -> Agent:
    """Instantiate an agent by name. Raises KeyError if unknown."""
    cls = _AGENTS[name]
    return cls()


def registered_agents() -> list[str]:
    return sorted(_AGENTS)
