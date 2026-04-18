-- Auto-run on first Postgres container init.
-- Alembic migrations assume these extensions exist.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
