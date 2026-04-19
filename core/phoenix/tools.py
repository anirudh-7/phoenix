"""Tool registry.

A "tool" is the implementation of an action_type. When the orchestrator
decides an action should run (policy = AUTO, or approval granted), it
looks up the tool in this registry and calls it.

Tools are async functions with a uniform signature:

    async def my_tool(args: dict[str, Any]) -> dict[str, Any]: ...

They return a result dict (stored in `actions.result`) or raise on failure.
The orchestrator catches exceptions and records them in `actions.error`.

For v1 we register tools at import time. Later we'll build a per-agent
registry so Mercury can't accidentally call a Mars tool.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

Tool = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_REGISTRY: dict[str, Tool] = {}


def register(action_type: str, tool: Tool) -> None:
    """Register a tool for an action_type. Raises if already registered."""
    if action_type in _REGISTRY:
        raise ValueError(f"tool already registered: {action_type}")
    _REGISTRY[action_type] = tool


def get(action_type: str) -> Tool | None:
    """Look up a tool. Returns None if no implementation is registered."""
    return _REGISTRY.get(action_type)


def registered_action_types() -> list[str]:
    """All registered action types (useful for diagnostics)."""
    return sorted(_REGISTRY)


# ---------------------------------------------------------------------------
# Built-in tools
# ---------------------------------------------------------------------------


async def notify_push(args: dict[str, Any]) -> dict[str, Any]:
    """No-op notification tool.

    For v1 we just print to stdout — later this becomes a real push (ntfy,
    Pushover, or a websocket to the dashboard). The shape of the args dict
    is the contract we want to keep stable.
    """
    title = args.get("title", "Phoenix")
    message = args.get("message", "")
    print(f"[notify] {title}: {message}")
    return {"delivered": True, "channel": "stdout"}


register("notify.push", notify_push)
