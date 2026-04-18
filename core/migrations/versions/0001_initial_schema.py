"""initial schema — frozen v1

The four layers: execution state, observability, audit, agent memory.
See /outputs/phoenix-schema-v1.sql for the DDL with full commentary.

Revision ID: 0001
Revises:
Create Date: 2026-04-18
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        -- =====================================================================
        -- Shared: updated_at maintenance trigger
        -- =====================================================================
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at := NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        -- =====================================================================
        -- Layer 1: Execution state
        -- =====================================================================

        CREATE TABLE agents (
            name          TEXT PRIMARY KEY,
            version       TEXT NOT NULL,
            description   TEXT NOT NULL,
            enabled       BOOLEAN NOT NULL DEFAULT TRUE,
            config        JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        COMMENT ON TABLE agents IS 'Registry of registered agents. Upserted from Python on startup.';

        CREATE TRIGGER trg_agents_updated BEFORE UPDATE ON agents
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();

        CREATE TYPE run_status AS ENUM (
            'queued', 'running', 'awaiting_approval', 'done', 'failed', 'cancelled'
        );

        CREATE TYPE trigger_type AS ENUM (
            'manual', 'scheduled', 'webhook', 'subagent'
        );

        CREATE TABLE runs (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            agent_name      TEXT NOT NULL REFERENCES agents(name),
            parent_run_id   UUID REFERENCES runs(id),
            trigger         trigger_type NOT NULL,
            trigger_context JSONB NOT NULL DEFAULT '{}'::jsonb,
            status          run_status NOT NULL DEFAULT 'queued',
            status_reason   TEXT,
            input           JSONB NOT NULL DEFAULT '{}'::jsonb,
            output          JSONB,
            error           JSONB,
            started_at      TIMESTAMPTZ,
            finished_at     TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX idx_runs_agent_status ON runs(agent_name, status);
        CREATE INDEX idx_runs_status_updated ON runs(status, updated_at)
            WHERE status IN ('queued', 'running', 'awaiting_approval');
        CREATE INDEX idx_runs_parent ON runs(parent_run_id) WHERE parent_run_id IS NOT NULL;

        COMMENT ON TABLE runs IS 'One row per agent invocation. The central execution-state table.';
        COMMENT ON COLUMN runs.parent_run_id IS 'Non-null when spawned as a subagent. Cycle prevention in app code.';

        CREATE TRIGGER trg_runs_updated BEFORE UPDATE ON runs
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();

        CREATE TYPE step_kind AS ENUM (
            'think', 'tool_call', 'observation', 'finish'
        );

        CREATE TABLE steps (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id         UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            step_number    INT NOT NULL,
            kind           step_kind NOT NULL,
            llm_model      TEXT,
            llm_input      JSONB,
            llm_output     JSONB,
            tokens_input   INT,
            tokens_output  INT,
            latency_ms     INT,
            checkpoint     JSONB,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (run_id, step_number)
        );

        CREATE INDEX idx_steps_run ON steps(run_id, step_number);

        CREATE TYPE policy_decision AS ENUM ('auto', 'approval', 'blocked');

        CREATE TYPE action_status AS ENUM (
            'pending', 'awaiting_approval', 'approved', 'executing',
            'succeeded', 'failed', 'rejected', 'cancelled'
        );

        CREATE TABLE actions (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id          UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            step_id         UUID NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
            agent_name      TEXT NOT NULL,
            action_type     TEXT NOT NULL,
            args            JSONB NOT NULL DEFAULT '{}'::jsonb,
            policy_decision policy_decision NOT NULL,
            policy_reason   TEXT,
            status          action_status NOT NULL DEFAULT 'pending',
            result          JSONB,
            error           JSONB,
            approved_by     TEXT,
            approved_at     TIMESTAMPTZ,
            executed_at     TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX idx_actions_run ON actions(run_id);
        CREATE INDEX idx_actions_status ON actions(status)
            WHERE status IN ('pending', 'awaiting_approval', 'executing');
        CREATE INDEX idx_actions_action_type ON actions(action_type, created_at DESC);

        COMMENT ON TABLE actions IS 'One row per tool call. Policy gate writes policy_decision before execution.';

        CREATE TRIGGER trg_actions_updated BEFORE UPDATE ON actions
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();

        CREATE VIEW approval_queue AS
            SELECT a.*, r.agent_name AS run_agent, r.trigger_context
            FROM actions a
            JOIN runs r ON r.id = a.run_id
            WHERE a.status = 'awaiting_approval'
            ORDER BY a.created_at ASC;

        -- =====================================================================
        -- Layer 2: Observability — event stream
        -- =====================================================================

        CREATE TABLE events (
            id          BIGSERIAL PRIMARY KEY,
            kind        TEXT NOT NULL,
            run_id      UUID REFERENCES runs(id) ON DELETE CASCADE,
            step_id     UUID REFERENCES steps(id) ON DELETE CASCADE,
            action_id   UUID REFERENCES actions(id) ON DELETE CASCADE,
            payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX idx_events_run ON events(run_id, id);
        CREATE INDEX idx_events_kind ON events(kind, created_at DESC);

        COMMENT ON TABLE events IS 'Append-only event stream for the dashboard. Retention 90 days.';
        COMMENT ON COLUMN events.kind IS
            'Intentionally TEXT, not ENUM. Source of truth is phoenix.events.EventKind in Python.';

        CREATE OR REPLACE FUNCTION notify_event() RETURNS TRIGGER AS $$
        BEGIN
            PERFORM pg_notify('phoenix_events', NEW.id::text);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_events_notify
            AFTER INSERT ON events
            FOR EACH ROW EXECUTE FUNCTION notify_event();

        -- =====================================================================
        -- Layer 3: Audit log — hash-chained, append-only
        -- =====================================================================

        CREATE TABLE audit_log (
            id           BIGSERIAL PRIMARY KEY,
            prev_hash    BYTEA NOT NULL,
            hash         BYTEA NOT NULL,
            actor        TEXT NOT NULL,
            action_type  TEXT NOT NULL,
            target       TEXT,
            data         JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX idx_audit_created ON audit_log(created_at DESC);
        CREATE INDEX idx_audit_actor ON audit_log(actor, created_at DESC);

        COMMENT ON TABLE audit_log IS
            'Tamper-evident, hash-chained, append-only. Hash computed in phoenix.audit (app-side).';

        -- =====================================================================
        -- Layer 4: Agent memory (Mercury v1)
        -- =====================================================================

        CREATE TYPE email_category AS ENUM (
            'personal', 'work', 'transactional', 'newsletter', 'promotional', 'noise'
        );

        CREATE TABLE sender_classifications (
            sender_email       TEXT PRIMARY KEY,
            last_category      email_category NOT NULL,
            confidence         REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
            email_count        INT NOT NULL DEFAULT 0,
            user_corrections   INT NOT NULL DEFAULT 0,
            last_seen          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX idx_sender_category ON sender_classifications(last_category);

        COMMENT ON TABLE sender_classifications IS
            'Mercury memory: per-sender learned category. Mercury is sole writer.';

        CREATE TRIGGER trg_sender_classifications_updated BEFORE UPDATE ON sender_classifications
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();

        CREATE TABLE vip_contacts (
            email        TEXT PRIMARY KEY,
            added_by     TEXT NOT NULL CHECK (added_by IN ('manual', 'auto')),
            reply_count  INT NOT NULL DEFAULT 0,
            notes        TEXT,
            added_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TYPE pending_event_status AS ENUM (
            'pending', 'consumed_by_jupiter', 'dismissed'
        );

        CREATE TABLE pending_events (
            id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_agent         TEXT NOT NULL,
            source_run_id        UUID REFERENCES runs(id),
            source_email_id      TEXT,
            proposed_title       TEXT,
            proposed_start       TIMESTAMPTZ,
            proposed_end         TIMESTAMPTZ,
            proposed_attendees   TEXT[],
            proposed_location    TEXT,
            raw_evidence         TEXT,
            status               pending_event_status NOT NULL DEFAULT 'pending',
            created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX idx_pending_events_status ON pending_events(status, created_at DESC);

        COMMENT ON TABLE pending_events IS
            'Mercury writes proposed calendar events here for Jupiter to consume later.';

        CREATE TABLE oauth_credentials (
            provider                TEXT NOT NULL,
            account_identifier      TEXT NOT NULL,
            access_token_encrypted  BYTEA NOT NULL,
            refresh_token_encrypted BYTEA,
            key_version             INT NOT NULL DEFAULT 1,
            expires_at              TIMESTAMPTZ,
            scopes                  TEXT[] NOT NULL,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (provider, account_identifier)
        );

        COMMENT ON TABLE oauth_credentials IS
            'Encrypted OAuth tokens. Encryption key unwrapped at boot. key_version enables rotation.';

        CREATE TRIGGER trg_oauth_credentials_updated BEFORE UPDATE ON oauth_credentials
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS oauth_credentials CASCADE;
        DROP TABLE IF EXISTS pending_events CASCADE;
        DROP TYPE IF EXISTS pending_event_status;
        DROP TABLE IF EXISTS vip_contacts CASCADE;
        DROP TABLE IF EXISTS sender_classifications CASCADE;
        DROP TYPE IF EXISTS email_category;
        DROP TABLE IF EXISTS audit_log CASCADE;
        DROP VIEW IF EXISTS approval_queue CASCADE;
        DROP TABLE IF EXISTS events CASCADE;
        DROP TABLE IF EXISTS actions CASCADE;
        DROP TYPE IF EXISTS action_status;
        DROP TYPE IF EXISTS policy_decision;
        DROP TABLE IF EXISTS steps CASCADE;
        DROP TYPE IF EXISTS step_kind;
        DROP TABLE IF EXISTS runs CASCADE;
        DROP TYPE IF EXISTS trigger_type;
        DROP TYPE IF EXISTS run_status;
        DROP TABLE IF EXISTS agents CASCADE;
        DROP FUNCTION IF EXISTS notify_event();
        DROP FUNCTION IF EXISTS set_updated_at();
        """
    )
