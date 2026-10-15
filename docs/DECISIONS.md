# Decisions

## Local Postgres: Docker Compose first, pgserver fallback (M1.1)
- **Chose:** `docker compose up -d postgres` (`pgvector/pgvector:pg16`, bound to 127.0.0.1:5432). An init SQL script creates the `orbit_test` database and the `vector` extension in both DBs. `scripts/dev_postgres.py` (pgserver) remains for machines without Docker.
- **Why:** Docker became available on the dev machine after planning. Compose gives the same image as CI and production, and pgserver stays as a zero-admin fallback.
- **When it fails:** if the Docker daemon is down, Orbit can't connect at startup and `/health` reports `db: error`. Start Docker Desktop, or run `python scripts/dev_postgres.py` and point `DATABASE_URL` at the URI it prints.
