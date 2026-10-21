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
4. A cheap keyword prefilter skips obvious senior and PhD-only roles without an LLM call, and only postings whose `content_hash` changed are ever re-extracted.
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

## Notifications (`orbit/radar/notify.py`, `Watcher.on_pushed`)
1. After scoring, the watcher claims every open posting at or above `notify_threshold` that has no `radar_notifications` row, with one `INSERT … ON CONFLICT (posting_id) DO NOTHING RETURNING`.
2. The UNIQUE `posting_id` is the "never twice" guarantee: once a row exists, no later re-score, close or reopen can claim that posting again.
3. Up to 5 matches become one Telegram message each (score, company, title, terms, locations, link) and the rest go into one digest, all sent through Orbit's existing `OrbitApp.push`.
4. The scheduler reports each push's success back through `Watcher.on_pushed`, which marks rows `sent` or `failed`; failed rows are retried on later polls, at most 3 pushes in total.
5. A row stuck in `pending` (the process died between claim and confirmation) is expired as "delivery unknown" and never re-sent: at-most-once delivery.

- **Q: Why at-most-once instead of at-least-once?** A: The cost of the two failure modes is lopsided. A missed alert is still visible in `orbit radar top` and chat search, while duplicate pings erode trust in the radar. Exactly-once isn't possible over Telegram's API, since a timeout can't tell you whether the message landed.
- **Q: How do two concurrent pollers avoid both sending the same posting?** A: The claim is an atomic insert against a UNIQUE constraint. Only one transaction's INSERT returns the row, so only that poller builds an event for it.

## Chat tools (`orbit/core/tools/radar.py`)
1. `search_postings(query, filters)` and `set_radar_goal(text)` are plain async functions with `@tool`; the registry turns their type hints and docstrings into the JSON schema the chat model sees.
2. The agent passes a `ToolContext` with the DB and (now) the router, so a tool can run SQL and call the `embed` task without importing app globals.
3. Filters are validated and normalized with the same helpers as extraction ("toronto" → "Toronto, ON", "AI" → `ml_ai`); a bad filter returns `error: …` so the model can retry.
4. With an embedding provider, the query is embedded and postings are ranked by pgvector cosine similarity; without one, a weighted whole-word keyword match ranks them instead.
5. Results come back as compact JSON (company, title, terms, locations, score, URL, notified), which the model turns into a short answer for "any new AI co-ops in Toronto?".

- **Q: Why return JSON from a tool instead of a formatted string?** A: The model is the formatter. Structured fields let it filter, summarize or answer follow-ups ("which of those are remote?") without re-parsing prose, and the `ranking` field lets it say how confident the ordering is.
- **Q: How do you test an LLM tool loop without calling an LLM?** A: A scripted fake provider returns a tool call first and a final answer second; the test asserts on the tool result the agent fed back. That checks the real registry, schema, DB query and agent loop deterministically.

## Pipeline, radar CLI and stats (`orbit/radar/pipeline.py`, `orbit/radar/cli.py`, `orbit/radar/stats.py`)
1. `run_pipeline` is the radar's single entry point: poll → extract → embed → score → notify, each stage in its own try/except so one failure (or a missing API key) never stops the others.
2. The 30-minute watcher and the `orbit radar poll/extract/embed` commands call the same functions, so manual runs behave exactly like the daemon.
3. Radar CLI commands open their own DB engine and router from `.env`/`config.yaml` (not the daemon's HTTP API), so they work while the daemon is down; tests swap that runtime for a test DB and fake providers.
4. `orbit radar stats` computes the project metrics with plain SQL: companies tracked, postings stored, per-field eval accuracy, notification latency (median/p90 via `percentile_cont`) and uptime days (distinct days with a successful poll).
5. Output is plain text made safe for non-UTF-8 Windows consoles, with `--json` for machine-readable use.

- **Q: Why isolate pipeline stages instead of letting an exception abort the run?** A: The stages have different failure causes (a board outage, an LLM quota, a missing resume). Isolating them means a broken embedding key still lets polling keep postings fresh and closes the ones that disappeared, and the summary shows exactly which stage failed.
- **Q: How is p90 latency computed in Postgres?** A: `percentile_cont(0.9) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM sent_at - first_seen_at))`, an ordered-set aggregate that interpolates between neighbouring values. Only `sent` rows count, since failed or expired rows have no real delivery time.

## Anthropic provider (`orbit/core/providers/anthropic.py`)
1. `AnthropicProvider` implements the same `Provider` protocol as the OpenAI one, so the router picks it per task from `config.yaml` (`type: anthropic`) and no call site changed.
2. It translates OpenAI-format messages: `system` turns become the top-level system prompt, `tool_calls` become `tool_use` blocks, and consecutive `tool` turns become `tool_result` blocks in one user turn.
3. Tools map to `{name, description, input_schema}` and `tool_choice` is never sent, so Claude decides when to call one; with no tools, tool history is sent as text.
4. The radar's `response_format` json_schema becomes structured outputs (`output_config.format`, through the SDK's `transform_schema`); `effort` maps to `output_config.effort`, and `cache_system` puts a `cache_control` breakpoint on the system prompt.
5. A turn that called tools keeps its raw content blocks (`provider_blocks`), so thinking blocks and their signatures go back verbatim in the next request, as the API requires; the OpenAI provider strips that key.

- **Q: Why must thinking blocks be sent back during a tool loop?** A: With thinking on, the API checks that the assistant turn holding `tool_use` still has the thinking that led to it (signed, so it can't be edited). Rebuilding the turn from text and tool calls alone loses it and the request is rejected.
- **Q: When does prompt caching actually save money?** A: Only when the cached prefix is at least the model's minimum (512 tokens on Haiku 5.5) and the same prefix repeats within the cache lifetime (5 minutes by default). Then reads cost 0.1× input, but the first write costs 1.25×, so a prefix used once costs more than no caching.

## Batch re-extraction (`orbit/radar/reextract.py`, `orbit radar reextract`)
1. After the `extract` route changes model, every open posting whose extraction is `ok` is sent again as one Anthropic Message Batch, built with the same request as live extraction (`AnthropicProvider.build_request`), at half the token price.
2. The batch id and each posting's `content_hash` go into kv (`radar:reextract`) before waiting, so an interrupted run resumes the same batch instead of paying twice.
3. Results are matched back by `custom_id` (`posting-<id>`) and written with the usual content_hash guard; an errored or invalid result keeps the old extraction, since a batch has no retry round.
4. If a field that feeds the embedding text changed (terms, locations, role_type, tech_stack), `embedded_hash` is cleared so `embed_pending` re-embeds that posting; then `score_postings` rescores everything.
5. It never notifies: the watcher's next poll does, and `radar_notifications.posting_id` is UNIQUE, so a posting already notified is never sent again.

- **Q: When is a batch API the right call instead of concurrent requests?** A: When nobody waits on the answer: a backfill or re-run of hundreds of items. It halves the price and avoids rate-limit juggling, at the cost of latency (minutes to hours, up to 24 h), so the live 30-minute poll keeps using normal requests.
- **Q: How do you make an asynchronous batch job safe to interrupt?** A: Persist the job id and the inputs' versions before waiting, make the collect step idempotent (it writes only where the content hash still matches, then clears the saved state), and on restart resume the saved job instead of submitting a new one.

## Domain packs (`orbit/packs/`, `orbit/core/tools/packs.py`, `packs/`)
1. A pack is a folder: `pack.yaml` declares collections as JSON Schemas, cron routines and locked skill sections; `SKILL.md` holds the domain's rules in `## ` sections.
2. Enabled packs load at startup; a pack with bad YAML, an invalid schema or cron, or no SKILL.md is logged and skipped, so one mistake never takes Orbit down.
3. All records share one `records` table (JSONB `data` + `schema_version`); the tools validate against the schema and return every violation to the model so it can fix the call.
4. The chat prompt only lists packs; the model calls `load_skill` when it needs a domain's rules, and `write_skill` rewrites one section (never a locked one), keeping the old file in `.history/`.
5. Routines run the same tool loop as chat with the pack's skill in the system prompt, and their reply is pushed to Telegram and added to the chat history.

- **Q: Why one JSONB table instead of a table per collection?** A: A new collection becomes a YAML change instead of a migration, and JSON Schema still validates every write in the app. The trade-off is weaker database-level guarantees and query plans, which is fine at personal data scale; a hot collection can still get its own table or an expression index later.
- **Q: How do you change a record schema without breaking old data?** A: Bump `schema_version`, store the version on every new record, and never rewrite stored rows. Readers branch on the version (or a migration writes new rows from old ones), and the export manifest carries each schema so old files stay interpretable.

## Data export (`orbit/export.py`, `orbit export`, `orbit import`)
1. A snapshot is plain files: JSONL for rows, Markdown for skills, memory and persona, YAML for pack schemas, the media folder, plus README.md and manifest.json.
2. The manifest records the export time, Orbit version and git commit, row counts and every collection's schema, so the snapshot explains itself without Orbit's code.
3. Import is idempotent: records keep their ids (`ON CONFLICT DO NOTHING`, then the id sequence is moved past them), memory lines and media files are only added when missing.
4. A nightly job writes a dated snapshot plus a `pg_dump` into `EXPORT_DIR` (a host bind mount in Docker) and prunes everything but the newest 14.
5. The two formats cover two failures: the dump restores the same stack exactly and fast, and the plain files survive a change of database, framework or model.

- **Q: Why both pg_dump and a plain-file export?** A: The dump is exact and quick to restore, but only into Postgres with this schema. The plain files are slower to restore but readable by any tool, so they outlive a change of stack.
- **Q: How do you make an import idempotent?** A: Give every row a stable identity (here the original id), insert with `ON CONFLICT DO NOTHING`, then reset the sequence past the imported ids; for files, copy only what is missing and back up anything you overwrite.

## Incognito mode (`orbit/core/incognito.py`, `OrbitApp.handle`)
1. `/incognito` turns a chat incognito (kv keeps only the expiry time), `/incognito off` or 30 idle minutes turn it off, and a `??` prefix makes one message incognito.
2. An incognito turn uses an in-process deque as its history and never touches the messages table, so the nightly export cannot contain it.
3. Only read-only tools are offered, and `ToolRegistry.call(..., allow=...)` refuses everything else, so even a model that ignores the prompt cannot write memory, skills or records.
4. Telegram photos arrive as bytes; `OrbitApp` saves them to `data/media` only for normal turns and passes them from memory otherwise.
5. Logs record only lengths and tool names for incognito turns, and every incognito reply starts with 🕶 so the user can see the mode.

- **Q: Why enforce "no writes" in the tool registry instead of telling the model?** A: The prompt is a request, not a guarantee: the persona tells the model to remember things proactively, and models sometimes call tools they were told not to. Filtering both the schemas offered and the calls executed makes the guarantee independent of the model.
- **Q: Why persist the on/off flag but not the conversation?** A: The flag is metadata the user relies on: losing it on a restart would silently start recording a chat they think is private. The conversation is exactly what must not be persisted, so losing it on restart is the intended failure.

## Skill change log (`orbit/packs/skills.py`, `orbit/cron.py`)
1. `write_skill` appends `{id, time, pack, section, reason, old, new}` to `data/skill_changes.jsonl` after each edit.
2. `list_skill_changes(since?)` reads it newest first; `revert_skill_change(id)` writes the old body back (or removes an added section).
3. A revert only applies if the section still equals that change's `new` text, otherwise it names the later changes to undo first.
4. The revert is itself a logged change (`reverts: id`), and locked sections are refused for reverts too.
5. `cron_trigger` turns crontab weekday numbers into names, because APScheduler 3 counts Monday as 0.

- **Q: Why refuse a revert when the section changed later, instead of just writing the old text?** A: Writing it would silently drop the later edit. Requiring reverts in reverse order is the same rule as `git revert` conflicts: only undo a change whose result is still what is on disk.
- **Q: Why an append-only JSONL file and not a table?** A: Skills are files, not database rows; the log lives next to them, is readable with `grep`/`jq` without Orbit, and is copied into the export as-is. Append-only means a crash mid-write loses at most the last line.

## Public and private packs (`Settings.pack_roots`, `load_packs`, `scripts/privacy_check.sh`)
1. Packs load from `packs/` (public, in git) and `private-packs/` (env `ORBIT_PRIVATE_PACKS`, git-ignored); a private pack with the same name wins.
2. A pack missing from both roots is logged and skipped, so a fresh clone without private data still starts.
3. Export records which root each pack came from; import puts it back there, and into the private root when unsure.
4. Tests use neutral fixture packs, so CI never needs the private ones.
5. `privacy_check.sh` fails on tracked or staged files containing a term from `private-packs/.privacy-terms`; it runs as a pre-commit hook.

- **Q: Why a second directory instead of a private branch or submodule?** A: The private content changes daily and is edited by Orbit itself; a plain git-ignored folder needs no git operations, keeps one code path, and is mounted into Docker like other runtime data. A submodule would still need a private remote and careful syncing.
- **Q: Does deleting a file and adding it to .gitignore make a repo safe to publish?** A: No. The file is still in every earlier commit. You either publish a fresh repository from the cleaned tree or rewrite history (git filter-repo) and force-push, and treat anything already pushed publicly as leaked.

## Publishing a public mirror (`scripts/publish_public.sh`, `.publicignore`)
1. Development happens only in the private repo; the public repo is a mirror regenerated at each release tag.
2. The script clones `main` fresh and runs `git filter-repo` to drop private paths from every commit and redact private terms in contents and messages.
3. Terms are replaced as whole words with regex lookarounds, so `PGPASSWORD`-style identifiers containing a term survive.
4. The result is checked with `privacy_check.sh --history` (every commit's diff and message) plus a removed-path scan; any hit aborts.
5. Nothing is pushed until a typed `yes`; main goes with `--force-with-lease` and the tag must be new.

- **Q: Why rewrite a clone instead of the private repo itself?** A: Rewriting changes every commit id, which breaks open PRs, CI history and every other clone. A throwaway clone gives the public a clean history while the private repo keeps its full, honest one.
- **Q: What does --force-with-lease protect against here?** A: The mirror's history is rewritten each release, so a plain fast-forward push is impossible. A lease on the value just fetched means the push fails if someone or something changed the mirror in between, instead of silently overwriting it.

## Packaging and deployment (`Dockerfile`, `docker-compose.yml`, `deploy/`)
1. The image is `python:3.12-slim` running as uid 1000. Its entrypoint runs `alembic upgrade head` and then `exec orbit serve`, so migrations apply before Orbit starts and SIGINT reaches Orbit for a clean shutdown.
2. Compose runs `orbit` and `pgvector/pgvector:pg16`. Orbit waits for Postgres's healthcheck, publishes only on 127.0.0.1, and has its own `/health` healthcheck.
3. `.env` is optional for Compose, so CI can validate the file, and `environment` overrides the host-oriented `DATABASE_URL` inside the container.
4. The EC2 kit (`deploy/`) is prepared but never run by agents. An idempotent script installs Docker, refuses unsafe `.env` files, backs up before each deploy, waits for `/health` and can roll back with `--ref`.
5. The production override drops the published Postgres port, adds restart policies, log rotation and memory limits, and nightly `pg_dump` backups are verified with `pg_restore --list`.

- **Q: Why `exec` in the entrypoint and `STOPSIGNAL SIGINT`?** A: Without `exec`, the shell is PID 1 and swallows signals. Orbit only shuts down cleanly on KeyboardInterrupt, so the container sends SIGINT and Orbit gets to stop the scheduler and close the DB.
- **Q: Why bind ports to 127.0.0.1 even on a server?** A: Nothing needs inbound access. Telegram long polling is outbound only, and the operator reaches `/health` through an SSH tunnel. Not listening publicly removes a whole class of exposure.
