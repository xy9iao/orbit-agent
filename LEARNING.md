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

## Sources (`orbit/radar/sources/`, `scripts/record_fixture.py`)
1. One adapter per job board (Greenhouse, Lever, Ashby) turns that board's public JSON into a common `NormalizedPosting`; a small registry maps the `source` column to the adapter.
2. All requests go through `PoliteClient`: a descriptive User-Agent, at most 4 requests in flight, at least 1 s between requests to the same host, and retries with exponential backoff on 429/5xx that honor `Retry-After`.
3. HTML descriptions are converted to readable text with the stdlib HTML parser, keeping paragraph and bullet breaks, so the LLM and the embeddings see clean text.
4. `content_hash()` covers only the fields extraction reads (title, location, department, description), so a changed URL or reordered JSON doesn't trigger a re-extraction.
5. Tests replay real recorded responses (trimmed by `record_fixture.py`, the only code that calls the live APIs) through `httpx.MockTransport`, and an autouse guard fails any test that tries to reach the network.

- **Q: Why honor `Retry-After` and space requests per host instead of just retrying quickly?** A: These are free public APIs. Hammering them risks an IP ban that would take the whole radar down, and polling every 30 minutes has plenty of slack, so politeness costs nothing that matters.
- **Q: How do you keep fixture tests honest when the real APIs change shape?** A: Fixtures are real recordings, not hand-written guesses, and `verify-sources` runs the adapters live against every seeded board, so drift shows up as a failed company rather than silently passing tests.

## Extraction (`orbit/radar/models.py` PostingExtraction, `orbit/radar/extract.py`)
1. Each new posting's title, company, location and (truncated) description go to the `extract` router task, which returns JSON constrained by a strict JSON Schema generated from the `PostingExtraction` Pydantic model.
2. Pydantic validators then normalize the model's output into canonical values ("Summer 2027", "Toronto, ON", "Remote (Canada)", lowercase tech names) and reject anything they can't normalize.
3. On a JSON or validation error the call is retried once with the error text fed back; a second failure, or any provider error, records `extraction_status = 'failed'` with the error message.
4. A cheap keyword prefilter skips obvious senior roles without an LLM call, and only postings whose `content_hash` changed are ever re-extracted.
5. No DB connection is held during the LLM call, and the result is written only if the posting's content hash is still the one that was sent.

- **Q: Why both a strict JSON schema and Pydantic validation?** A: Strict mode guarantees the shape (keys and types), but not the meaning: the model can still write "summer '27" or "Toronto Ontario". Validators turn that into canonical values the SQL filters can compare exactly, and reject output that can't be normalized.
- **Q: Why write with `WHERE content_hash = :hash`?** A: It's optimistic concurrency. The LLM call takes seconds, and if a poll updated the posting meanwhile, storing the old extraction would label new content with stale fields. The guarded UPDATE affects zero rows, and the posting stays pending for the next run.

## Polling (`orbit/radar/{seed,verify,repo,poll,watcher}.py`)
1. `seeds.yaml` lists the companies and their public board tokens; `orbit radar seed` upserts them and `verify-sources` fetches each board live to prove the token works.
2. The `internship_radar` watcher runs every 30 minutes inside Orbit's APScheduler (60 s after startup, never overlapping) and calls the same `poll_all` as `orbit radar poll`.
3. `poll_all` fetches all companies concurrently through the polite client; each company gets a `radar_runs` row that is written before the fetch and finalized after it.
4. A successful fetch is synced in one transaction: `INSERT … ON CONFLICT (source, external_id) DO UPDATE` dedupes, changed content resets extraction, missing postings are closed and returning ones reopen.
5. A failed fetch rolls back and closes nothing, so a flaky board can never wipe its postings; the error is stored on the run row.

- **Q: How do you know which rows `INSERT … ON CONFLICT DO UPDATE` inserted vs updated?** A: `RETURNING (xmax = 0) AS inserted`: a freshly inserted row has no deleting/updating transaction id, while an updated row's old version sets `xmax`. Combined with a pre-read of the old hashes, that also tells changed from unchanged rows.
- **Q: Why must a failed fetch never close postings?** A: "Missing from the response" only means "gone" when the response is complete. Closing on a 500 or timeout would mark every posting closed and then reopen them all on the next poll, churning state and stats.

## Eval (`orbit/radar/eval.py`, `evals/radar_labels.yaml`)
1. A YAML label file holds 30 real posting snapshots with hand-written expected values for the seven extraction fields; until a human reviews it, its status says UNVERIFIED and every report prints a warning banner.
2. `orbit radar eval` runs the same `extract_posting` call production uses on each snapshot (3 at a time) and compares the result field by field.
3. Booleans and the role enum score by exact match; list fields (terms, locations, tech stack) score by exact set equality after normalization, with mean Jaccard overlap as a softer second number.
4. A failed extraction counts as wrong on every field, but an outage (3 provider errors in a row) aborts without saving, so it can't masquerade as 0% accuracy.
5. The summary is stored in kv `radar:last_eval`, and `orbit radar stats` reads it for the per-field accuracy metric.

- **Q: Why report per-field accuracy instead of one overall score?** A: Fields fail differently: `is_internship` is usually easy, while `terms` and `locations` depend on messy text and normalization. Per-field numbers show exactly which prompt rule or validator to fix.
- **Q: Why both exact set match and Jaccard for list fields?** A: Exact match is the honest bar (a missing location can break a filter), but it's harsh on long lists like tech stacks. Jaccard shows whether the model is close or way off.

## Matching (`orbit/radar/profile.py`, `orbit/radar/match.py`)
1. Your resume (and an optional goal sentence) is embedded once through the router's `embed` task and stored in `radar_profile`; it's re-embedded only when its text hash changes.
2. Each extracted internship posting gets an embedding of its title, company, extracted fields and description, stored in a pgvector `vector(1536)` column.
3. Rule filters run in SQL on the JSONB extraction: open, `is_internship`, a term you want (or none stated), and a location you want (or remote and Canada-eligible).
4. Eligible postings are scored in one UPDATE as `1 - (embedding <=> resume_embedding)` (cosine similarity), blended 0.8/0.2 with goal similarity when a goal exists; postings that stop qualifying have their score cleared.
5. `orbit radar top N` lists the best open matches, with a line marking the notify threshold, so you can see which postings would be pushed.

- **Q: Why filter with rules before ranking by similarity?** A: Embeddings measure topical closeness, not eligibility. A senior ML role in San Francisco can sit very close to an ML resume. Hard constraints (internship, term, location) belong in exact filters, and similarity only ranks what's left.
- **Q: Why compute cosine similarity in Postgres instead of in Python?** A: pgvector's `<=>` runs where the data lives, so one UPDATE scores every posting without moving thousands of 1536-float vectors over the wire. The same operator later works with an HNSW index if the table grows.
