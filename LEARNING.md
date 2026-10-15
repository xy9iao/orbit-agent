# Learning notes

Per module: a 5-line explanation, then 2 interview questions with short answers.

## Storage on Postgres (`orbit/storage/`: models.py, db.py, migrate.py, migrations/; `scripts/migrate_sqlite_to_pg.py`)
1. All tables are SQLAlchemy 2 declarative models in one `MetaData` with a naming convention, so Alembic autogenerate and `alembic check` can diff the code against the live schema.
2. `Database` wraps an async engine (asyncpg) and keeps the old SQLite-era API: methods return plain dicts with naive local ISO timestamps, so no caller changed.
3. Each write uses `engine.begin()` (one short transaction, auto-commit on exit); kv upserts use `INSERT … ON CONFLICT DO UPDATE` and store values as JSONB.
4. Migrations are hand-written Alembic revisions; `OrbitApp.start()` runs `upgrade head` in-process, and the test fixture migrates once per session, then `TRUNCATE … RESTART IDENTITY` before each test.
5. The one-off SQLite → Postgres script copies rows with their original ids using `ON CONFLICT DO NOTHING` (so re-runs are safe), then `setval`s each sequence to `MAX(id)`.

- **Q: Why `setval` after copying rows with explicit ids?** A: Inserting explicit ids doesn't advance a SERIAL/IDENTITY sequence, so the next normal insert would reuse id 1 and fail on the primary key. `setval` moves the sequence past the copied max.
- **Q: Why truncate between tests instead of wrapping each test in a rolled-back transaction?** A: The `Database` methods open and commit their own transactions, so an outer rollback can't contain them without rewiring every method onto a shared connection. TRUNCATE is fast on small tables and makes isolation obvious; RESTART IDENTITY keeps ids deterministic.

## CI (`.github/workflows/ci.yml`)
1. Every push and PR runs one Ubuntu job with a `pgvector/pgvector:pg16` service container, so tests hit the same Postgres + pgvector as production.
2. The job installs the package with dev extras (pip cache keyed on `pyproject.toml`), runs `ruff check .`, then `alembic upgrade head` on a clean database.
3. pytest then runs against the same service DB; the fixture's own migration is a no-op there, and tests fail rather than skip if the DB is missing.
4. `docker compose config -q` validates the compose file without building images.
5. `concurrency` cancels superseded runs on the same branch, the token is read-only, and a 15-minute timeout bounds hung runs.

- **Q: Why run `alembic upgrade head` as its own step when the tests migrate anyway?** A: It proves the migration chain applies to an empty database and gives a clear, separate failure signal before any test output.
- **Q: How do service containers reach the job?** A: GitHub starts them on a shared Docker network and maps the declared ports to the runner host, so the job connects to `localhost:5432`. Health-check options make the job wait until Postgres accepts connections.
