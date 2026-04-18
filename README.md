# Phoenix

A local-first, event-driven multi-agent personal assistant.

Seven agents, named for the classical planets and Roman gods:

- **Mercury** — messenger (Gmail, messaging)
- **Mars** — decisive action (Zerodha)
- **Sol** — morning briefing, daily digest
- **Luna** — overnight maintenance and async work
- **Venus** — relationships, DMs, soft tasks
- **Jupiter** — the agent of agents (planning, oversight)
- **Saturn** — boundaries, policy, retrospectives

v1 scope: Mercury (Gmail triage) and Mars (Zerodha read-only portfolio) only.

## Architecture

- **Core** (Python/FastAPI) — orchestrator, agent registry, policy engine, tool execution.
- **Ashes** (Postgres 17 + pgvector) — state, checkpoints, memory, audit log, event stream.
- **Nest** (Next.js 15) — dashboard, approval queue, run inspector.
- **Ember** (Go, deferred) — webhook gateway and long-lived connection holder.
- **Cinder** (CLI) — admin ops, token refresh, log tailing.

## Quickstart

```bash
# 1. Start Postgres
docker compose up -d

# 2. Install Python deps and sync
cd core
uv sync

# 3. Apply schema migration
uv run alembic upgrade head

# 4. Verify
docker compose exec db psql -U phoenix -d phoenix -c '\dt'
```

## Project docs

- `/infra/policy.yaml` — action policy (whitelist autonomy)
- `/core/migrations/` — database schema evolution

Documentation lives in the planning doc outside the repo for now.
