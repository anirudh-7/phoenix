"""Add NOTIFY trigger on events table.

Every INSERT into events fires `pg_notify('phoenix_events', NEW.id::text)`.
The payload is just the primary key — consumers do a follow-up SELECT to
fetch the full row. That keeps payloads well under Postgres's 8KB NOTIFY
cap regardless of how big our JSONB payloads grow.

The channel name is hard-coded ('phoenix_events'). If we ever run
multiple Phoenix instances against the same database (we won't, but just
in case) we'd parameterize this per-instance. For a local-first
deployment the fixed name is simpler and fine.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION phoenix_notify_event() RETURNS trigger AS $$
        BEGIN
            PERFORM pg_notify('phoenix_events', NEW.id::text);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER events_notify_after_insert
            AFTER INSERT ON events
            FOR EACH ROW
            EXECUTE FUNCTION phoenix_notify_event();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS events_notify_after_insert ON events;")
    op.execute("DROP FUNCTION IF EXISTS phoenix_notify_event();")
