"""SQLAlchemy 2.0 ORM models mirroring the schema.

Alembic does not autogenerate from these — we write raw-SQL migrations. Models
are runtime vocabulary only. If the migration and the model drift, the model
is wrong; the migration is the source of truth.

All table names, column names, enum values, and types match
core/migrations/versions/0001_initial_schema.py exactly.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ---------------------------------------------------------------------------
# Python-side enums that mirror the Postgres ENUM types.
# These drive both typing and serialization.
# ---------------------------------------------------------------------------


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TriggerType(StrEnum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    WEBHOOK = "webhook"
    SUBAGENT = "subagent"


class StepKind(StrEnum):
    THINK = "think"
    TOOL_CALL = "tool_call"
    OBSERVATION = "observation"
    FINISH = "finish"


class PolicyDecision(StrEnum):
    AUTO = "auto"
    APPROVAL = "approval"
    BLOCKED = "blocked"


class ActionStatus(StrEnum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class EmailCategory(StrEnum):
    PERSONAL = "personal"
    WORK = "work"
    TRANSACTIONAL = "transactional"
    NEWSLETTER = "newsletter"
    PROMOTIONAL = "promotional"
    NOISE = "noise"


class PendingEventStatus(StrEnum):
    PENDING = "pending"
    CONSUMED_BY_JUPITER = "consumed_by_jupiter"
    DISMISSED = "dismissed"


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Declarative base. One per project."""


# Helper: bind a Python StrEnum to an existing Postgres ENUM type.
def pg_enum(enum_cls: type[StrEnum], name: str) -> postgresql.ENUM:
    return postgresql.ENUM(
        enum_cls,
        name=name,
        create_type=False,                           # migration already created it
        values_callable=lambda x: [e.value for e in x],
    )


# ---------------------------------------------------------------------------
# Layer 1: execution state
# ---------------------------------------------------------------------------


class Agent(Base):
    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    agent_name: Mapped[str] = mapped_column(
        Text, ForeignKey("agents.name"), nullable=False
    )
    parent_run_id: Mapped[UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("runs.id")
    )
    trigger: Mapped[TriggerType] = mapped_column(
        pg_enum(TriggerType, "trigger_type"), nullable=False
    )
    trigger_context: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB, nullable=False, default=dict, server_default="{}"
    )
    status: Mapped[RunStatus] = mapped_column(
        pg_enum(RunStatus, "run_status"),
        nullable=False,
        default=RunStatus.QUEUED,
        server_default=RunStatus.QUEUED.value,
    )
    status_reason: Mapped[str | None] = mapped_column(Text)
    input: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB, nullable=False, default=dict, server_default="{}"
    )
    output: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB)
    started_at: Mapped[datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True)
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    parent: Mapped[Run | None] = relationship(
        "Run", remote_side="Run.id", back_populates="children"
    )
    children: Mapped[list[Run]] = relationship(
        "Run", back_populates="parent"
    )
    steps: Mapped[list[Step]] = relationship(
        "Step", back_populates="run", cascade="all, delete-orphan"
    )
    actions: Mapped[list[Action]] = relationship(
        "Action", back_populates="run", cascade="all, delete-orphan"
    )


class Step(Base):
    __tablename__ = "steps"
    __table_args__ = (UniqueConstraint("run_id", "step_number"),)

    id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    run_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[StepKind] = mapped_column(
        pg_enum(StepKind, "step_kind"), nullable=False
    )
    llm_model: Mapped[str | None] = mapped_column(Text)
    llm_input: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB)
    llm_output: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB)
    tokens_input: Mapped[int | None] = mapped_column(Integer)
    tokens_output: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB)
    created_at: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    run: Mapped[Run] = relationship("Run", back_populates="steps")


class Action(Base):
    __tablename__ = "actions"

    id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    run_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB, nullable=False, default=dict, server_default="{}"
    )
    policy_decision: Mapped[PolicyDecision] = mapped_column(
        pg_enum(PolicyDecision, "policy_decision"), nullable=False
    )
    policy_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ActionStatus] = mapped_column(
        pg_enum(ActionStatus, "action_status"),
        nullable=False,
        default=ActionStatus.PENDING,
        server_default=ActionStatus.PENDING.value,
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB)
    approved_by: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True)
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    run: Mapped[Run] = relationship("Run", back_populates="actions")


# ---------------------------------------------------------------------------
# Layer 2: observability
# ---------------------------------------------------------------------------


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE")
    )
    step_id: Mapped[UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("steps.id", ondelete="CASCADE")
    )
    action_id: Mapped[UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("actions.id", ondelete="CASCADE")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Layer 3: audit
# ---------------------------------------------------------------------------


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    prev_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Layer 4: agent memory
# ---------------------------------------------------------------------------


class SenderClassification(Base):
    __tablename__ = "sender_classifications"
    __table_args__ = (CheckConstraint("confidence BETWEEN 0 AND 1"),)

    sender_email: Mapped[str] = mapped_column(Text, primary_key=True)
    last_category: Mapped[EmailCategory] = mapped_column(
        pg_enum(EmailCategory, "email_category"), nullable=False
    )
    confidence: Mapped[float] = mapped_column(nullable=False, default=0.5)
    email_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    user_corrections: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class VipContact(Base):
    __tablename__ = "vip_contacts"
    __table_args__ = (CheckConstraint("added_by IN ('manual', 'auto')"),)

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    added_by: Mapped[str] = mapped_column(String(16), nullable=False)
    reply_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class PendingEvent(Base):
    __tablename__ = "pending_events"

    id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    source_agent: Mapped[str] = mapped_column(Text, nullable=False)
    source_run_id: Mapped[UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("runs.id")
    )
    source_email_id: Mapped[str | None] = mapped_column(Text)
    proposed_title: Mapped[str | None] = mapped_column(Text)
    proposed_start: Mapped[datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True)
    )
    proposed_end: Mapped[datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True)
    )
    proposed_attendees: Mapped[list[str] | None] = mapped_column(postgresql.ARRAY(Text))
    proposed_location: Mapped[str | None] = mapped_column(Text)
    raw_evidence: Mapped[str | None] = mapped_column(Text)
    status: Mapped[PendingEventStatus] = mapped_column(
        pg_enum(PendingEventStatus, "pending_event_status"),
        nullable=False,
        default=PendingEventStatus.PENDING,
        server_default=PendingEventStatus.PENDING.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class OAuthCredential(Base):
    __tablename__ = "oauth_credentials"

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    account_identifier: Mapped[str] = mapped_column(Text, primary_key=True)
    access_token_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    refresh_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True)
    )
    scopes: Mapped[list[str]] = mapped_column(postgresql.ARRAY(Text), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
