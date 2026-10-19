# Orbit

![CI](https://github.com/xy9iao/orbit/actions/workflows/ci.yml/badge.svg?branch=radar-mvp)

Orbit is an always-on personal AI agent. I talk to it from my phone through Telegram and from my
laptop through a CLI, and both share one memory. It is built from small, readable modules rather
than an agent framework: a model router (`config.yaml` maps each task to a provider and model), a
tool registry (`@tool`), in-process APScheduler jobs (reminders, a morning briefing) and a Watcher
plugin interface for background polling.

**Internship Radar** is Orbit's first real vertical. A watcher tracks public job postings from 31
tech companies hiring in Canada (Canadian companies plus a few US ones with large Canadian
engineering offices) through the Greenhouse, Lever and Ashby public job-board APIs, extracts
structured fields with an LLM (is it an internship, which terms, where, remote, role type, tech
stack), scores each posting against my resume with pgvector, and pushes new high-scoring matches to
Telegram once and never twice. In chat I can ask things like *"any new AI co-ops in Toronto?"* and
Orbit answers through the `search_postings` tool.

## Highlights

- **Idempotent polling.** Postings are upserted with `INSERT ... ON CONFLICT (source, external_id)`,
  a content hash decides whether a posting really changed, postings missing from a *successful*
  fetch are closed and reopened if they come back. A failed fetch closes nothing.
- **Schema-constrained extraction.** The JSON schema is generated from a Pydantic model and sent as
  a strict `response_format`. Normalizing validators clean up terms, locations and tech stacks, one
  retry feeds the validation error back, and a keyword prefilter skips obvious senior and PhD-only roles with no
  LLM call. `orbit radar eval` measures per-field accuracy against a labeled set.
- **pgvector matching.** Hard rules (open, internship, term overlap, location or remote-in-Canada)
  run as SQL filters; survivors are ranked by cosine similarity to the resume embedding, blended
  0.8/0.2 with an optional goal profile.
- **At-most-once notifications.** Each match is claimed with `INSERT ... ON CONFLICT (posting_id) DO
  NOTHING RETURNING` before it is pushed, so a re-score, restart or re-poll can never send it twice.
  Failed pushes retry up to 3 times.
- **Async SQLAlchemy 2 + Alembic** on PostgreSQL for all of Orbit (messages, reminders, usage, kv and
  the radar tables). Every schema change is a migration, and migrations run on startup.
- **Docker Compose** for `orbit` + `postgres` (pgvector), with a `/health` endpoint that reports DB
  status. An EC2 deploy script and runbook are prepared.
- **CI.** GitHub Actions runs ruff, `alembic upgrade head` on a clean database and the full pytest
  suite against a pgvector service container on every push. Tests use fake LLM, embedding and
  Telegram clients plus recorded API fixtures, never the live network.

## Architecture

```mermaid
flowchart TB
    me(("Me"))

    subgraph channels["Channels"]
        tg["Telegram bot"]
        cli["CLI + local API"]
    end

    agent["Agent loop"]
    tools["Tools: reminders, memory,<br/>search_postings, set_radar_goal"]
    router["Model Router<br/>task to provider/model"]
    llm["Anthropic Messages API<br/>+ OpenAI-compatible API"]

    subgraph sched["APScheduler, in-process"]
        jobs["Reminders + briefing"]
        watcher["internship_radar watcher<br/>every 30 min"]
    end

    subgraph pipeline["Radar pipeline"]
        direction LR
        poll["poll"] --> extract["extract"] --> embed["embed"] --> score["score"] --> notify["notify"]
    end

    subgraph boards["Public job-board APIs"]
        gh["Greenhouse"]
        lv["Lever"]
        ab["Ashby"]
    end

    polite["PoliteClient<br/>rate limit + retries"]
    db[("Postgres + pgvector<br/>messages, reminders, usage, kv,<br/>radar tables")]

    me <--> tg
    me <--> cli
    tg --> agent
    cli --> agent
    agent --> router
    router --> llm
    agent --> tools
    agent --> db
    tools --> db
    cli -.->|"orbit radar ..."| db

    watcher --> poll
    poll --> polite
    polite --> gh
    polite --> lv
    polite --> ab
    extract -.->|"extract task"| router
    embed -.->|"embed task"| router
    poll --> db
    score --> db
    notify -->|"push once, never twice"| tg
    jobs -->|"push"| tg
```

- **Channels → Agent → Router.** A Telegram message or a CLI request (the CLI talks to a FastAPI
  server on `127.0.0.1:8765` inside the daemon) goes to the agent loop, which keeps per-chat history
  in Postgres, calls the router's `chat` task with the tool schemas, runs tool calls and loops until
  the model answers. Code only names a *task* (`chat`, `extract`, `embed`, ...); `config.yaml` picks
  the provider and model.
- **Scheduler → Watcher → Pipeline.** The `internship_radar` watcher runs every 30 minutes: poll every
  active board through the polite HTTP client, extract pending postings (capped per poll), embed new
  internships, rescore with SQL filters + pgvector, then claim and push new matches. Each stage is
  isolated, so a missing API key or a provider outage skips that stage without stopping polling.
- **`orbit radar ...` commands** open their own DB connection and router, so batch jobs (verify,
  eval, top, stats) work while the daemon is down.

## Setup

You need Python 3.12 and a PostgreSQL 16 server with the pgvector extension (the Compose file
provides one). Secrets live only in `.env` (git-ignored); copy `.env.example` and fill in:

| Variable | Needed for |
|---|---|
| `ANTHROPIC_API_KEY` | Claude: chat and briefing (Opus 5.5), extraction and code (Sonnet 5.5), fast and summarize (Haiku 5.5) |
| `OPENAI_API_KEY` | embeddings (`embed` task, text-embedding-3-small) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CHAT_ID` | the Telegram channel and all pushes. Without them Orbit runs CLI-only. |
| `DATABASE_URL` | the Postgres URL (Compose sets its own for the container) |
| `TEST_DATABASE_URL` | pytest only |
| `PROXY_URL` (optional) | an HTTP proxy for OpenAI and Telegram, e.g. in mainland China |

**Telegram bot:** create a bot with [@BotFather](https://t.me/BotFather) to get the token, and get
your chat id from @userinfobot. Orbit only answers the owner chat id; if it is wrong, send the bot
`/id` and it replies with the id to put in `.env`.

Migrations run automatically: the container runs `alembic upgrade head` before `orbit serve`, and
`orbit serve` migrates on start too.

### Option 1: Docker Compose

```bash
cp .env.example .env            # fill OPENAI_API_KEY and the TELEGRAM_* keys
docker compose up -d --build    # postgres (pgvector) + orbit
curl http://127.0.0.1:8765/health
# {"status": "ok", "db": "ok", "db_latency_ms": ..., "version": ..., "uptime_seconds": ...}
docker compose logs -f orbit
```

Both ports are published on 127.0.0.1 only. `memory/`, `persona/`, `data/` and `logs/` are mounted
from the repo, so `data/resume.md` on the host is visible to the container. Run radar commands
inside the container with `docker compose exec orbit orbit radar <command>`.

> **PROXY_URL inside Docker:** `127.0.0.1` in the container is the container itself, so
> `PROXY_URL=http://127.0.0.1:7890` breaks OpenAI and Telegram calls there. Use
> `http://host.docker.internal:7890` in Docker, and leave it unset on EC2.

### Option 2: Local Windows with Task Scheduler

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env                  # fill the keys; DATABASE_URL already points at 127.0.0.1:5432
docker compose up -d postgres           # just the database
.venv\Scripts\orbit serve               # run the daemon in the foreground (Ctrl+C to stop)
```

No Docker? `pip install pgserver` and run `python scripts\dev_postgres.py` instead: it starts a
local Postgres with pgvector in `data/pgserver` and creates the `orbit` and `orbit_test` databases
(`python scripts\dev_postgres.py --stop` stops it).

To keep Orbit running in the background, register it as a scheduled task (from an elevated
PowerShell). It starts at logon and restarts one minute after a crash:

```powershell
.\scripts\install_task.ps1
.\scripts\uninstall_task.ps1            # remove it
```

Logs go to `logs/orbit.log`. Set the Windows power plan to never sleep, or Orbit goes offline when
the machine does.

**Upgrading from the SQLite version:** `python scripts\migrate_sqlite_to_pg.py` copies an old
`data/orbit.db` (messages, reminders, usage, kv) into `DATABASE_URL` once. It keeps ids and is safe
to re-run.

### Option 3: AWS EC2

[`deploy/RUNBOOK.md`](deploy/RUNBOOK.md) covers instance sizing, the security group (SSH only),
first deploy with `deploy/ec2_deploy.sh`, updates, rollback, backups (`deploy/backup.sh`) and
secret rotation. These scripts are **prepared, not executed**: nothing has been deployed to AWS yet.

## Daily use

```powershell
orbit chat                      # interactive chat (shares memory with Telegram); /clear, /quit
orbit send "remind me to drink water in 10 minutes"
orbit status                    # uptime, channels, model routes, today's token usage
orbit reminders                 # pending reminders; `orbit cancel <id>` cancels one
orbit jobs                      # scheduled jobs and their next run
orbit briefing                  # generate and push today's briefing now
orbit push "hello"              # push raw text to Telegram, no model
orbit export / orbit import DIR # plain-file snapshot of your data (works without the daemon)
```

These talk to the running daemon over the local API. `orbit radar ...` commands do not need the
daemon.

## Domain packs: training and meals over Telegram

A pack is a folder, `packs/<name>/`, that teaches Orbit one area of life without new Python code:

- `pack.yaml`: `name`, `description`, `schema_version`, `collections` (name -> JSON Schema of one record),
  `routines` (`{cron, prompt}`, local time) and `locked_sections` (SKILL.md sections Orbit may not edit).
- `SKILL.md`: the pack's rules in `## ` sections. Orbit reads it with `load_skill` before working in
  that area and updates one section with `write_skill` when you correct it (the old file goes to
  `.history/`; locked sections are refused).

Enabled packs (`packs.enabled` in `config.yaml`) load at startup; a broken pack is logged and skipped.
The chat prompt lists every pack, and records go into one `records` table (`pack`, `collection`,
`occurred_at`, `data` JSONB, `schema_version`) through `add_record`, `query_records`, `update_record`,
`delete_record` and `record_stats` (count/sum/min/max/first/last per day, week, month or field).

Two packs ship today:

| Pack | Collection | Routines |
|---|---|---|
| `fitness` | `workouts`: day_type, exercise, sets (weight, unit, reps, rir, assist), pain, energy, notes | 07:30 Tue-Sun: today's plan with last session's numbers. Sun 21:00: weekly summary. |
| `nutrition` | `meals`: meal, items (name, amount, protein_g, kcal), protein_g, kcal, notes | 21:30 daily: protein vs 140-160 g, one suggestion if short. |

Tell Orbit what you trained or ate ("卧推 80kg 8,8,7 RIR 2", "午饭鸡胸 200 g 加一碗米饭") and it logs it
and confirms in one line. Photos work too: they are saved under `data/media/YYYY-MM-DD/`, sent to the
model as an image, and the path goes into the record.

### Your data, outside Orbit

```powershell
orbit export [--out DIR]   # plain-file snapshot (JSONL, Markdown, YAML) + README.md + manifest.json
orbit import DIR           # restore records, skills, memory and media; rows already present are skipped
```

Every night at 03:00 Orbit writes `orbit-YYYY-MM-DD/` plus a `pg_dump` (`orbit-YYYY-MM-DD.dump`) into
`EXPORT_DIR` and keeps the newest 14. In Docker that folder is bind-mounted to `./exports` on the host
(`EXPORT_HOST_DIR` changes it). A snapshot needs no Orbit code to read; see its README.md.

## Internship Radar quick start

```powershell
orbit radar seed                    # load the 31 companies from orbit/radar/seeds.yaml into the DB
orbit radar verify-sources          # fetch every board live; exits 1 if fewer than 20 pass
# save your resume as plain text or Markdown at data/resume.md (git-ignored)
orbit radar resume                  # embed it as the radar profile (needs OPENAI_API_KEY)
orbit radar poll                    # fetch and sync all boards once (no API key needed)
orbit radar extract                 # LLM extraction of pending postings
orbit radar embed                   # embed extracted internships
orbit radar top 10                  # best open matches with scores and the notify_threshold cut line
orbit radar stats                   # the metrics below
```

With the daemon running you can skip `poll`, `extract` and `embed`: the `internship_radar` watcher
runs poll → extract → embed → score → notify every 30 minutes (first run 60 s after start) and
pushes new matches above `radar.notify_threshold` to Telegram.

**Calibrating the threshold.** `notify_threshold` is calibrated at `0.50` from real scores
(technical internships scored 0.48–0.57 against the author's resume and goal). After changing your
resume or goal, run `orbit radar top 30`, find the weakest posting you would still want pushed, try cut
points with `orbit radar top 30 --threshold 0.45`, and set `radar.notify_threshold` just below that
score.

In chat, `search_postings` answers questions such as "any new AI co-ops in Toronto?" (semantic
ranking when an embedding provider is configured, keyword ranking otherwise), and `set_radar_goal`
stores a free-text goal ("ML infrastructure, Toronto, summer 2027") that is blended into the score.

### `orbit radar` commands

| Command | What it does |
|---|---|
| `orbit radar seed [--file PATH] [--prune]` | Upsert the seed companies by slug. `--prune` deactivates companies that are no longer in the file. |
| `orbit radar verify-sources [--source S] [--min-ok N]` | Fetch each active company's board live, print ok/error and posting counts, store the result. Exits 1 below `--min-ok` (default 20). |
| `orbit radar resume [PATH]` | Embed a `.md`/`.txt` resume as the profile (default `radar.resume_path`). Skips re-embedding when the text is unchanged. |
| `orbit radar poll [--company SLUG ...] [--source S]` | Poll the boards once and sync postings: new, updated, closed, reopened. |
| `orbit radar extract [--retry-failed] [--limit N]` | Run LLM extraction on pending open postings, newest first. |
| `orbit radar embed [--limit N]` | Embed extracted internships whose embedding is missing or stale. |
| `orbit radar reextract [--limit N] [--no-wait]` | After the extract model changes: re-extract every open, already extracted posting in one Anthropic Message Batch (half price), then re-embed what changed and rescore. Running it again resumes the batch in flight. Never notifies. |
| `orbit radar eval [--labels PATH] [--dry-run]` | Per-field extraction accuracy against `evals/radar_labels.yaml`; saved for `stats`. `--dry-run` only loads the labels. |
| `orbit radar top [N] [--threshold X] [--new]` | Rescore, then print the best N open matches (default 10). `--new` hides already-notified postings. |
| `orbit radar stats [--json]` | Print the radar metrics. Read-only, no API key needed. |

`--source` takes `greenhouse`, `lever` or `ashby`. In Docker, run any of these as
`docker compose exec orbit orbit radar <command>` (the image includes `evals/`).

### Configuration: the `radar:` section of `config.yaml`

| Key | Default | Meaning |
|---|---|---|
| `terms` | `["Winter 2027", "Summer 2027"]` | Work terms you want, as `"<Season> <YYYY>"`. Postings whose terms are unstated still pass. |
| `locations` | Toronto, Waterloo, Kitchener, `Remote (Canada)` | A posting passes if an extracted location is listed, or it is remote and open to Canada. Format `"City, XX"`. |
| `role_types` | `[swe, ml_ai, infra, research]` | Hard filter on the extracted role type; empty means any role. Unknown names fail at load. |
| `notify_threshold` | `0.50` (calibrated) | Minimum score that gets pushed to Telegram (see above). |
| `resume_path` | `data/resume.md` | Resume file, relative to `ORBIT_HOME`. |
| `max_extract_per_poll` | `40` | Caps LLM extraction cost per watcher run. |
| `max_notifications_per_poll` | `5` | Individual Telegram messages per run; the rest go into one digest message. |

The watcher itself is enabled under `watchers:` (`type: internship_radar, interval_minutes: 30`).
The models come from the `extract` (claude-sonnet-5-5 at low effort; gpt-6-luna and claude-haiku-5-5
are the commented alternatives) and `embed` (text-embedding-3-small, 1536 dims) tasks under `tasks:`.

## Metrics

`orbit radar stats` prints these five metrics. Numbers below are only ones actually observed during
the build (see the log in [PROGRESS.md](PROGRESS.md)). So far the build has run without an OpenAI
key or Telegram credentials, so the metrics that need them are still pending.

| Metric | Status |
|---|---|
| Companies tracked | **31** companies hiring in Canada: 14 on Ashby, 11 on Greenhouse, 6 on Lever. `verify-sources` live run: **31/31 OK** (1,957 postings listed, 49 with intern titles). |
| Postings stored | A live `orbit radar poll` of all 31 boards fetched and stored **1,957 postings in 14.1 s**. An immediate re-poll of the 14 Ashby boards gave **0 new, 0 updated, 0 closed** (idempotent). |
| Extraction accuracy per field | **Provisional** (30 labels, still *UNVERIFIED, needs human review*). 2026-10-16, gpt-6-luna / claude-haiku-5-5 / claude-sonnet-5-5, all low effort: is_internship 100 / 100 / 100%, terms 93.3 / 90.0 / 100%, locations 86.7 / 80.0 / 83.3%, remote_ok 93.3 / 96.7 / 93.3%, canada_eligible 100 / 96.7 / 100%, role_type 93.3 / 86.7 / 93.3%, tech_stack 86.7 / 80.0 / 83.3%. |
| Posting-to-notification latency | **Pending.** Median and p90 of `sent_at - first_seen_at`; accrues once the daemon runs with OpenAI and Telegram keys. |
| Uptime days | **Pending.** Distinct days with at least one successful radar run; accrues once the daemon runs continuously. |

`orbit radar stats` also shows postings by status and extraction state, runs in the last 24 hours
and today's extract/embed token usage. `--json` prints the same data as JSON.

## Testing and CI

```powershell
# a Postgres + pgvector test database; its name must contain "test"
$env:TEST_DATABASE_URL = "postgresql+asyncpg://orbit:orbit@127.0.0.1:5432/orbit_test"
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check .
```

- **554 tests** as of the last build step. They run against a real Postgres because SQLite cannot test
  pgvector, JSONB or `ON CONFLICT`. The fixture runs `alembic upgrade head` once per session and
  truncates every table before each test. It fails loudly, not skips, when `TEST_DATABASE_URL` is
  missing, and refuses a database whose name does not contain `test`. The Compose init script
  creates `orbit_test` for you.
- **No network in tests.** LLM, embedding and Telegram clients are fakes, and the job-board adapters
  are tested against real responses recorded with `scripts/record_fixture.py`
  (`tests/fixtures/radar/`) through `httpx.MockTransport`; the HTTP-facing radar tests patch the real
  transport so any request that escapes the mock fails the test.
- **End-to-end test** (`tests/radar/test_e2e.py`): recorded Greenhouse/Lever/Ashby fixtures → a real
  `OrbitApp` and scheduler → extraction → embeddings → scores → exactly two fake Telegram messages;
  a second run sends nothing and makes no LLM calls.
- **CI** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs on every push and pull request
  with a `pgvector/pgvector:pg16` service container: `ruff check .`, `alembic upgrade head` on a clean
  database, `pytest`, then `docker compose config -q`.

## Configuration files

- `.env`: secrets and machine-specific values (never committed).
- `config.yaml`: providers, the task → model table, agent limits, the local API address, jobs,
  watchers and the `radar:` section. A provider is `type: anthropic` (the Anthropic Messages API) or
  `type: openai` (the default): any OpenAI-compatible endpoint (DeepSeek, Ollama, vLLM) works by
  adding a provider with its `base_url`.
- `persona/system.md`: Orbit's system prompt. `persona/skills/*.md`: extra prompts loaded per task
  (for example the briefing).
- `memory/MEMORY.md`: long-term memory that Orbit maintains itself through the `remember` and
  `forget` tools (git-ignored).
- `orbit/radar/seeds.yaml`: the tracked companies (slug, name, board type, board token, city).

## Project layout

```
orbit/
  app.py                 OrbitApp: builds and wires everything once
  channels/              Telegram, local FastAPI server (Channel protocol)
  cli/main.py            `orbit` CLI (Typer); mounts `orbit radar`
  core/                  agent loop, model router, providers, tools (@tool registry)
  scheduler/             APScheduler wrapper, briefing job, Watcher base + registry
  storage/               async SQLAlchemy models, Database, Alembic migrations
  packs/                 domain pack loader, records + stats, SKILL.md section editing
  export.py              `orbit export` / `orbit import` and the nightly snapshot
  radar/                 Internship Radar
    sources/             Greenhouse, Lever, Ashby adapters + PoliteClient
    repo.py, poll.py     upsert/dedupe/close, per-company poll runs
    extract.py, eval.py  LLM extraction, accuracy harness
    profile.py, match.py resume/goal embeddings, SQL filters + pgvector scoring
    notify.py            at-most-once notifications
    pipeline.py, watcher.py, stats.py, cli.py
tests/                   pytest suite; tests/radar/ for the radar, fixtures/ for recorded API responses
evals/radar_labels.yaml  extraction eval set (UNVERIFIED drafts)
packs/                   domain packs: fitness/, nutrition/ (pack.yaml + SKILL.md)
scripts/                 Task Scheduler install, dev Postgres, SQLite migration, fixture recorder
deploy/                  EC2 deploy script, prod compose override, backup, runbook
```

## Extending Orbit

| To add | Do this |
|---|---|
| A tool | Write a function in `orbit/core/tools/`, decorate it with `@tool` (schema from type hints and the docstring; a `ctx` parameter receives the `ToolContext`), and import it in `orbit/core/tools/__init__.py`. |
| A watcher (Gmail, RSS, a web page) | Subclass `Watcher` from `orbit/scheduler/watchers/base.py`, register it with `@register_watcher("name")`, and enable it under `watchers:` in `config.yaml`. Optional hooks: `bind(orbit)`, `on_pushed(event, ok)`, `close()`. |
| A life domain (sleep, reading, finances) | Add `packs/<name>/pack.yaml` (collections as JSON Schemas, routines, locked sections) and `SKILL.md`, then list it in `packs.enabled`. No code needed. |
| A channel (Discord, web) | Implement the `Channel` protocol in `orbit/channels/base.py` and register it in `orbit/app.py`. |
| A job board | Add an adapter in `orbit/radar/sources/` that returns `NormalizedPosting`s, register it in `sources/__init__.py`, add the source name to `RADAR_SOURCES` in `orbit/storage/models.py` plus an Alembic migration for its CHECK constraints, and record a fixture with `scripts/record_fixture.py`. |
| A model or provider | Edit `config.yaml` only. Changing the embedding model or dimension needs a migration and a re-embed. |

## Docs

- [PLAN.md](PLAN.md): the Internship Radar MVP plan, acceptance criteria and architecture notes.
- [docs/DECISIONS.md](docs/DECISIONS.md): what was chosen, why, and what happens when it fails.
- [LEARNING.md](LEARNING.md): per-module explanations with interview questions.
- [deploy/RUNBOOK.md](deploy/RUNBOOK.md): EC2 deploy, update, backup and rollback (prepared, not executed).
- [evals/radar_labels.yaml](evals/radar_labels.yaml): the extraction eval set and how to review it.
- [QUESTIONS.md](QUESTIONS.md): open items that need a human (API keys, threshold calibration).
