# Decisions

## Local Postgres: Docker Compose first, pgserver fallback (M1.1)
- **Chose:** `docker compose up -d postgres` (`pgvector/pgvector:pg16`, bound to 127.0.0.1:5432). An init SQL script creates the `orbit_test` database and the `vector` extension in both DBs. `scripts/dev_postgres.py` (pgserver) remains for machines without Docker.
- **Why:** Docker became available on the dev machine after planning. Compose gives the same image as CI and production, and pgserver stays as a zero-admin fallback.
- **When it fails:** if the Docker daemon is down, Orbit can't connect at startup and `/health` reports `db: error`. Start Docker Desktop, or run `python scripts/dev_postgres.py` and point `DATABASE_URL` at the URI it prints.

## Core table column types (M1.3)
- **Chose:** `BIGINT` identity-style ids (`BIGSERIAL`), bounded `VARCHAR` for short identifiers (session 200, role 20, channel 50, chat_id 100, model 100, kv key 200), `TEXT` for message content and reminder text, kv values as `JSONB`, plus indexes on `messages(session, id)`, `reminders(status, due_at)` and `usage(created_at)`.
- **Why:** the indexes match the only query patterns (`get_history`, `list_reminders`/`reminders_between`, `usage_since`). Bounded identifiers catch garbage early, and JSONB lets later code query kv values.
- **When it fails:** a value longer than its bound raises a `DataError` on insert, which surfaces as an error in the agent or tool reply rather than as silent truncation. Widening a column is a one-line migration.
