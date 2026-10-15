# Decisions

## Local Postgres: Docker Compose first, pgserver fallback (M1.1)
- **Chose:** `docker compose up -d postgres` (`pgvector/pgvector:pg16`, bound to 127.0.0.1:5432). An init SQL script creates the `orbit_test` database and the `vector` extension in both DBs. `scripts/dev_postgres.py` (pgserver) remains for machines without Docker.
- **Why:** Docker became available on the dev machine after planning. Compose gives the same image as CI and production, and pgserver stays as a zero-admin fallback.
- **When it fails:** if the Docker daemon is down, Orbit can't connect at startup and `/health` reports `db: error`. Start Docker Desktop, or run `python scripts/dev_postgres.py` and point `DATABASE_URL` at the URI it prints.

## Core table column types (M1.3)
- **Chose:** `BIGINT` identity-style ids (`BIGSERIAL`), bounded `VARCHAR` for short identifiers (session 200, role 20, channel 50, chat_id 100, model 100, kv key 200), `TEXT` for message content and reminder text, kv values as `JSONB`, plus indexes on `messages(session, id)`, `reminders(status, due_at)` and `usage(created_at)`.
- **Why:** the indexes match the only query patterns (`get_history`, `list_reminders`/`reminders_between`, `usage_since`). Bounded identifiers catch garbage early, and JSONB lets later code query kv values.
- **When it fails:** a value longer than its bound raises a `DataError` on insert, which surfaces as an error in the agent or tool reply rather than as silent truncation. Widening a column is a one-line migration.

## Migrations run on startup as well as in the container entrypoint (M1.4)
- **Chose:** `OrbitApp.start()` calls `Database.migrate()`, which runs `alembic upgrade head` in-process through `orbit/storage/migrate.py` (a programmatic Alembic `Config` pointing at the packaged `migrations/`, no `alembic.ini` needed). The CLI `alembic` command still works for manual use.
- **Why:** the Windows Task Scheduler mode runs `orbit serve` directly with no entrypoint script, so without this a fresh install or an upgrade would start on a stale schema. `upgrade head` is idempotent and takes milliseconds when nothing is pending.
- **When it fails:** a broken migration stops Orbit at startup with the Alembic error in `logs/orbit.log`. That is intentional: running on a half-migrated schema is worse. Fix the migration, or `alembic downgrade -1` to roll back.

## Test database guard (M1.4)
- **Chose:** tests read `TEST_DATABASE_URL` from the environment or `.env`, fail (not skip) if it is missing, and refuse any URL whose database name does not contain `test`.
- **Why:** the fixture truncates every table before each test, and one mistyped URL would wipe real conversation history and reminders.
- **When it fails:** pytest stops immediately with a message that says what to set.

## Parallel tasks: one worktree and one test database each (CLAUDE.md parallelism rule)
- **Chose:** each parallel subagent works in its own git worktree, runs the main venv's Python with `python -m ...` from the worktree root (so the worktree's `orbit` package is imported before the editable install), and uses a private database `orbit_test_<task>` on the shared Postgres. Agents never edit PROGRESS/QUESTIONS/LEARNING/DECISIONS; the coordinator merges branches one at a time, runs the full suite after each merge, and writes the tracking docs.
- **Why:** the `db` fixture truncates every table, so two agents on one test DB would corrupt each other's runs. Keeping shared docs coordinator-only removes the main source of merge conflicts.
- **When it fails:** a worktree created from the wrong base (seen once: worktrees started from `main`) is caught by checking each branch's parent commit before merging. A merge conflict means the branch is rebased onto the new head and its tests are re-run before merging.

## CI details (M1.9)
- **Chose:** `DATABASE_URL` and `TEST_DATABASE_URL` both set at job level to the service DB `orbit_test`; an explicit `alembic upgrade head` step before pytest; a 15-minute job timeout; a 10×5s service health check.
- **Why:** the separate migration step gives a clear "migration failed" signal on a clean database before any test runs (the fixture's own upgrade is then a no-op). Without `DATABASE_URL`, Alembic would fall back to the `orbit` DB, which doesn't exist in CI, so it fails loudly instead of migrating the wrong place.
- **When it fails:** a red run blocks the merge. `docker compose config -q` passes today only because compose needs no `.env`. **M1.8 must keep that true** (`env_file` with `required: false`), or CI must create an empty `.env` first.

## Router embeddings details (M5.1)
- **Chose:** `ModelRouter.embed` uses the same `resolve()` as `complete` (an unknown task falls back to `chat`), never splits batches, and raises `RuntimeError` if the provider returns a different number of vectors than texts. `FakeProvider` records embed calls in a separate `embed_calls` list.
- **Why:** one resolution path keeps "code names a task, config picks the model". Callers (M5.3) store vectors by position, so a short response must never silently attach vectors to the wrong posting. Keeping chat and embed calls apart means existing tests that index `calls[0]` are unaffected.
- **When it fails:** a mistyped task name sends the embeddings request to the chat model and the API rejects it; the error surfaces to the caller. On a count mismatch the whole batch fails with no writes and no usage recorded, and the pipeline leaves those postings un-embedded (`embedded_hash != content_hash`) so the next poll retries them.

## /health semantics (M1.7)
- **Chose:** `OrbitApp.db_health()` pings with a 2 s `asyncio.wait_for` timeout and never raises; `/health` returns HTTP 503 with `status: "degraded"` and a short `"<ExceptionType>: <first line>"` error (≤200 chars) when the DB is down. `/status` gains `"db"` but still needs the DB for its other fields.
- **Why:** the Docker healthcheck and `curl -f` act on the status code, so `/health` must answer even when Postgres hangs, without leaking tracebacks.
- **When it fails:** a DB slower than 2 s reports as degraded (`TimeoutError`). On Windows a refused localhost connection takes about 2 s, so it can also show as a timeout. `/status` returns 500 while the DB is down, as it did before; use `/health` for liveness.

## SQLite → Postgres copy semantics (M1.6)
- **Chose:** rows whose id (or kv key) already exists in Postgres are skipped, not overwritten; all four tables and the sequence bumps are copied in one transaction, after reading the whole SQLite file; invalid JSON in kv aborts the run.
- **Why:** re-runs are safe and never clobber data Orbit wrote after the switch, and a partial copy is impossible.
- **When it fails:** if Postgres already holds unrelated rows with the same ids, those SQLite rows are skipped; the first run's "skipped" count shows it, so migrate into an empty DB. One bad row aborts the whole run with the error and writes nothing; fix the row and re-run.

## Radar schema details (M2.1)
- **No ANN index yet.** `<=>` queries on `radar_postings.embedding` run as exact scans. There will be a few thousand postings at most, an exact scan takes milliseconds, and exact search needs no tuning. *When it fails:* if scoring or search gets slow (around 100k rows), add an HNSW `vector_cosine_ops` index in a new migration; queries already use `<=>`, so no code changes.
- **No custom Alembic type comparison for `vector`.** pgvector 0.5.1 registers the type for reflection, and `alembic check` detected a deliberate `vector(3)` vs `vector(1536)` drift during testing. *When it fails:* if a pgvector release drops that registration, Alembic warns "did not recognize type 'vector'"; then add a narrow `compare_type` hook for VECTOR only.
- **Python-side timestamp defaults.** `created_at`, `first_seen_at`, `last_seen_at`, `started_at` and `updated_at` use `default=_now` (naive local, no microseconds) rather than `server_default=now()`, because Postgres in Docker runs in UTC while Orbit stores local time. The default also applies to Core `insert()` and `ON CONFLICT` statements. *When it fails:* raw SQL inserts must set these columns themselves, or NOT NULL raises.
- **Column choices.** Job-board text (title, url, location_raw, department, description_text) is `TEXT`, so long values never fail; adapters store `""` for missing location or description, since those columns are NOT NULL. Hashes are `VARCHAR(64)` (sha256 hex), and score is `DOUBLE PRECISION`. The composite unique constraint is named explicitly `uq_radar_postings_source_external_id`. *When it fails:* adding a fourth job board needs a migration that widens the `source` CHECK constraint.

## Profile embedding (M5.2)
- **Chose:** before hashing, the text is normalized (CRLF→LF, strip, files read as `utf-8-sig`). Re-embedding is skipped only when the hash matches *and* an embedding exists. No DB session is held open during the embed call. Text sent to the embedder is cut at 24k characters, while the full text is stored. Radar CLI commands record usage through `db.add_usage` like the daemon. The radar sub-app is mounted on `orbit` now (pulled forward from M2.5), with one `open_runtime` helper that later commands and tests reuse. Only `.md` and `.txt` resumes are accepted.
- **Why:** a resume saved on Windows and one saved on Unix must hash the same; a row missing its embedding repairs itself on the next run; embedding costs money, so it must show up in usage even outside the daemon; character truncation needs no tokenizer dependency.
- **When it fails:** non-UTF-8 files and a missing API key exit 1 with a one-line message. Text that tokenizes far denser than English could still exceed the model's token limit, and that API error currently surfaces as a traceback. If two processes write the same profile at once, the last write wins, which is harmless. Every `orbit` command now imports SQLAlchemy at startup, so the CLI starts slightly slower.

## Source adapters and polite HTTP (M2.2)
- **Chose:** each adapter exposes `board_request(board_token) -> (url, params)`, which the fixture recorder reuses. Requests take the global 4-slot semaphore, then wait for the per-host 1 s spacing; backoff sleeps happen outside the semaphore. 429/500/502/503/504 and transport errors retry with 1/2/4 s backoff (no jitter); other non-2xx responses (such as 404) raise immediately. A `Retry-After` over 60 s makes the client give up instead of retrying early. `content_hash` is sha256 over sorted-key JSON of the stripped title, location_raw, department and description_text; URL, ids, raw payload and timestamps are excluded. Fixtures keep the real response structure, put intern and co-op postings first, and are trimmed to ≤8 KB per job.
- **Why:** respecting the boards' limits matters more than speed at about 30 companies every 30 minutes. Hashing only the fields extraction reads means re-extraction happens only when the content that matters changes.
- **When it fails:** a company whose fetch keeps failing is recorded as a failed run (M3.2) and retried next interval, and its open postings are left untouched. Changing the hash's field set invalidates every stored hash, which triggers a one-time re-extraction of all postings. An adapter that needs paging would need a different recorder hook.

## Container details (M1.8)
- **Chose:** migrations ship as package data (`migrations/` has no `__init__.py`). The image keeps the `orbit/` source in `/app` next to the installed package, so `alembic.ini`'s script path works. Dependencies install in their own layer. The listen address is overridable through `ORBIT_API_HOST/ORBIT_API_PORT` in `Settings`, while the CLI still dials config.yaml's host. Compose `environment` pins `ORBIT_HOME=/app`, `DATABASE_URL` (service host `postgres`) and the port, because `environment` beats `env_file` and the host `.env` points at 127.0.0.1. The service also sets `TZ=${TZ:-America/Toronto}`. `STOPSIGNAL SIGINT` plus `exec orbit serve`. Non-root uid 1000. The healthcheck uses Python urllib (the slim image has no curl).
- **Why:** Orbit stores and schedules in naive local time, so a UTC container would fire the 08:00 briefing at 04:00 Toronto time. Orbit only shuts down cleanly on KeyboardInterrupt (SIGINT). uid 1000 matches the usual first Linux user (such as ec2-user), so bind mounts stay writable.
- **When it fails:** a new folder under `migrations/` needs its own package-data pattern, or startup stops with an Alembic "can't find scripts" error. On Linux, bind-mount folders auto-created by Docker are owned by root and Orbit can't write logs; the deploy script (M7.3) must `mkdir` and `chown 1000` `data/ logs/ memory/` first. A wrong `TZ` only shifts schedules.

## Posting sync semantics (M3.1)
- **Chose:** `sync_company` takes an `AsyncSession` and never commits. The extraction and score reset is decided inside the upsert SQL (`CASE WHEN content_hash = excluded.content_hash …`). The counts come from a `FOR UPDATE` pre-read and don't overlap (a closed posting that returns counts as reopened even if its content also changed). Only the hashed fields count as a change (a new URL or raw payload is stored without resetting extraction). A source mismatch raises `ValueError`. Fetched ids are passed as one array parameter, and the upsert is chunked at 500 rows. **An empty successful fetch closes all of the company's open postings.**
- **Why:** the caller (M3.2) keeps the posting writes and its `radar_runs` row in one transaction. Putting the reset in SQL keeps the stored data correct even if the pre-read count is stale. Array parameters avoid asyncpg's 32,767 bind-parameter limit on large boards.
- **When it fails:** a caller that forgets to commit silently loses the sync. If a board ever returns HTTP 200 with a wrongly empty list, its postings close, then reopen on the next good poll with their extraction kept, and they are never re-notified, because `radar_notifications` is untouched. Under a concurrent writer the counts can be off by one, but the data stays correct.

## Lever and Ashby mapping (M2.3, M2.4)
- **Chose:** a Lever board token written `eu:<site>` is fetched from the EU host. `location_raw` lists the distinct locations in board order, joined with "; ", plus " (Remote)" or " (Hybrid)" from the workplace flag ("Remote" alone when no location is given). Lever department is `categories.department`, falling back to `team`. Descriptions come from the plain-text fields, with HTML (not unescaped twice) as the fallback. `updated_at` is None: neither API exposes it, and change detection uses `content_hash` anyway. Ashby postings with `isListed: false` are dropped. The URL falls back to `applyUrl`.
- **Why:** EU support needs no schema change. Extraction (M4) needs every location and the remote signal in one string.
- **When it fails:** an EU company seeded without the `eu:` prefix 404s, and `verify-sources` shows it. Roles listed in many countries produce long location strings that M4.1's validators must normalize. Changing the location format later changes every Lever and Ashby `content_hash`, which re-extracts them all once. At some companies Lever's `department` is just the company name; the raw payload keeps `team`, so extraction can still use it.
