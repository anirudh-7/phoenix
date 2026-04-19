"""Event kinds.

This is the source of truth for valid values of `events.kind`.

The DB column is TEXT (not an ENUM) so that adding a new kind isn't a
migration. The cost is that Python enforces the vocabulary. Always use
these constants; never write a literal string into `events.kind`.
"""

from __future__ import annotations

from enum import StrEnum


class EventKind(StrEnum):
    # Run lifecycle
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_AWAITING_APPROVAL = "run.awaiting_approval"
    RUN_RESUMED = "run.resumed"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"

    # Step lifecycle
    STEP_STARTED = "step.started"
    STEP_COMPLETED = "step.completed"

    # Action lifecycle
    ACTION_PROPOSED = "action.proposed"
    ACTION_PENDING_APPROVAL = "action.pending_approval"
    ACTION_APPROVED = "action.approved"
    ACTION_REJECTED = "action.rejected"
    ACTION_EXECUTED = "action.executed"
    ACTION_FAILED = "action.failed"
    ACTION_BLOCKED = "action.blocked"
