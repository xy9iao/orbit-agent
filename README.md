# Orbit

**A self-hosted, always-on AI agent with a model router, tool registry, domain packs and an internship-radar watcher.**

[![CI](https://github.com/xy9iao/orbit-agent/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/xy9iao/orbit-agent/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

<!--
  Demo: add a screenshot or GIF here, for example
  <p align="center"><img src="docs/assets/demo.gif" alt="Orbit pushing an internship match to Telegram" width="720"></p>
-->

## Why

Most agent demos are a chat loop over one model. Orbit is a long-running process that lives on my
machine or on a small server: I talk to it from Telegram and a CLI with one shared memory, and it works
on its own schedule, so it can watch the world and message me first. It is built from small, readable
modules instead of an agent framework, so every piece (routing, tools, scheduling, storage) is code
you can read in one sitting.

## Architecture

```mermaid
flowchart TB
    user(("You"))

    subgraph channels["Channels"]
        tg["Telegram bot"]
        api["orbit CLI → local API (FastAPI)"]
    end

    subgraph core["Core"]
        agent["Agent loop<br/>history · tool calls · incognito"]
        packs["Domain packs<br/>pack.yaml + SKILL.md"]
        tools["Tool registry (@tool)<br/>memory · reminders · packs · radar"]
        router["Model router<br/>config.yaml: task → provider/model"]
    end

    subgraph providers["LLM providers"]
        anthropic["Anthropic Messages API"]
        openai["OpenAI-compatible API<br/>chat + embeddings"]
    end

    subgraph sched["APScheduler (in-process)"]
        jobs["Jobs<br/>reminders · briefing · pack routines · nightly export"]
        watcher["Watcher plugins<br/>internship_radar, every 30 min"]
    end

    radar["Internship Radar pipeline<br/>poll → extract → embed → score → notify"]
    boards["Greenhouse · Lever · Ashby<br/>public job-board APIs"]
    db[("PostgreSQL + pgvector")]

    user <--> tg
    user <--> api
    tg --> agent
    api --> agent
    packs --> agent
    agent --> tools
    agent --> router
    router --> anthropic
    router --> openai
    jobs --> agent
    watcher --> radar
    radar -->|rate-limited client| boards
    radar -.->|extract + embed tasks| router
    agent --> db
    tools --> db
    radar --> db
    radar -->|push once| tg
```

- **Channels to agent to router.** A Telegram message or a CLI request (the CLI talks to a FastAPI
  server on `127.0.0.1:8765` inside the daemon) goes to the agent loop. The loop keeps per-chat
  history in Postgres, calls the router's `chat` task with the tool schemas, runs tool calls and loops
  until the model answers.
- **Code names a task, config picks the model.** Code only asks for `chat`, `extract`, `embed`, and so
  on; `config.yaml` maps each task to a provider and model. Providers are the Anthropic Messages API or
  any OpenAI-compatible endpoint (OpenAI, DeepSeek, Ollama, vLLM).
- **Scheduler and watchers.** APScheduler runs reminders, the daily briefing, pack routines, the nightly
  export and every `Watcher` plugin in the same process. Each pipeline stage is isolated, so a missing
  API key or a provider outage skips that stage without stopping polling.

## Features

- **Internship Radar.** A watcher polls the Greenhouse, Lever and Ashby **public** job-board APIs
  through a rate-limited client, extracts structured fields with an LLM (internship or not, terms,
  locations, remote, role type, tech stack), scores postings against a resume embedding with pgvector,
  and pushes new matches to Telegram. In chat, `search_postings` answers questions like
  *"any new AI co-ops in Toronto?"*.
- **Idempotent polling.** Postings are deduplicated on `(source, external_id)` with
  `INSERT ... ON CONFLICT`; postings missing from a successful fetch are closed and reopened if they
  return; a failed fetch closes nothing.
- **At-most-once notifications.** Each match is claimed with `ON CONFLICT (posting_id) DO NOTHING
  RETURNING` before it is sent, so a restart, re-poll or re-score never pushes it twice.
- **Schema-constrained extraction.** The JSON schema comes from a Pydantic model and is sent as
  structured output; validators normalize the fields, and one retry feeds a validation error back.
  `orbit radar eval` scores each field against a labeled set, and `orbit radar reextract` redoes
  stored extractions in one Anthropic Message Batch after a model change.
- **Model router.** One table in `config.yaml` maps tasks to providers and models, with per-task
  parameters (reasoning effort, prompt caching). Swapping a model is a one-line config change.
- **Tool registry.** `@tool` builds the JSON schema from type hints and the docstring; tools cover
  long-term memory, reminders, pack records and skills, and radar search.
- **Domain packs.** A folder with `pack.yaml` (record collections as JSON Schema, cron routines) and an
  editable `SKILL.md` teaches Orbit a new area of life without Python code. Orbit edits one `## `
  section of `SKILL.md` when you correct it, and **locked sections** refuse edits.
- **Skill change log.** Every skill edit is appended to `data/skill_changes.jsonl` (time, pack,
  section, reason, old and new text), and you can ask Orbit to list or revert a change.
- **JSONL export and import.** `orbit export` writes a plain-file snapshot (JSONL, Markdown, YAML,
  plus a README and manifest); `orbit import` restores it. A nightly job writes a snapshot plus a
  `pg_dump`.
- **Incognito mode.** Send `/incognito`, or start a single message with `??`. Nothing is written
  to history, memory, skills or records, and logs show only message lengths. It ends after 30 minutes
  of silence.
- **Ops.** Docker Compose with a `/health` endpoint, Alembic migrations on startup, a Windows Task
  Scheduler install script, and EC2 deploy scripts with a runbook (prepared, not executed).

### By the numbers

All counted from this repository:

| | |
|---|---|
| Tests | **688** (pytest against a real Postgres + pgvector; no live network) |
| Companies in the radar seed list | **31** (14 Ashby, 11 Greenhouse, 6 Lever) in [`orbit/radar/seeds.yaml`](orbit/radar/seeds.yaml) |
| Extraction models compared with the eval harness | **3** (gpt-6-luna, claude-haiku-5-5, claude-sonnet-5-5), on a 30-posting labeled set in [`evals/radar_labels.yaml`](evals/radar_labels.yaml) |

## Extraction eval

`orbit radar eval` runs the configured `extract` model over a labeled set and scores each field
(`evals/radar_labels.yaml`, n=30). Latest run, model **claude-sonnet-5-5** (effort low), 0 failed
extractions:

| Field | Accuracy | Jaccard |
|---|---:|---:|
| is_internship | 100.0% | – |
| terms | 100.0% | 1.00 |
| locations | 86.7% | 0.93 |
| remote_ok | 96.7% | – |
| canada_eligible | 100.0% | – |
| role_type | 93.3% | – |
| tech_stack | 86.7% | 0.98 |

Accuracy is an exact match (set equality for list fields); Jaccard is the mean set overlap for list
fields. The labels are 30 real postings from the tracked boards, labeled by hand from the posting text
and checked against the source; the labeling rules are in the file's header.

## Quickstart

You need Docker with Compose. Orbit's chat and extraction run on Claude, embeddings on OpenAI.

```bash
git clone https://github.com/xy9iao/orbit-agent.git
cd orbit-agent
cp .env.example .env
```

Edit `.env`:

- set `ANTHROPIC_API_KEY` (chat, briefing, extraction) and `OPENAI_API_KEY` (embeddings);
- Telegram is optional: the `TELEGRAM_*` lines ship commented out, so Orbit starts CLI-only. To use it,
  uncomment them and fill `TELEGRAM_BOT_TOKEN` ([@BotFather](https://t.me/BotFather) gives the token)
  and `TELEGRAM_OWNER_CHAT_ID`.

Start Postgres (pgvector) and Orbit, then check health:

```bash
docker compose up -d --build
curl http://127.0.0.1:8765/health
# {"status":"ok","db":"ok","db_latency_ms":...,"version":"0.1.0","uptime_seconds":...}
```

Send a first message through the CLI:

```bash
docker compose exec orbit orbit send "hello"
docker compose exec orbit orbit chat        # interactive; /clear, /quit
docker compose exec orbit orbit status      # uptime, channels, model routes, scheduled jobs
```

Try the radar (these three need no API keys):

```bash
docker compose exec orbit orbit radar seed    # load the 31 companies
docker compose exec orbit orbit radar poll    # fetch every board once
docker compose exec orbit orbit radar stats
```

Migrations run automatically on start. Ports are published on `127.0.0.1` only; `POSTGRES_PORT` and
`ORBIT_PORT` change them. Local (non-Docker) setup, Windows Task Scheduler, EC2 and every CLI command
are in [docs/SETUP.md](docs/SETUP.md); the radar commands and settings are in
[docs/RADAR.md](docs/RADAR.md).

## Project layout

```
orbit/
  app.py            OrbitApp: builds and wires everything once
  channels/         Telegram bot and the local FastAPI server behind the CLI
  cli/              the `orbit` command (Typer)
  core/             agent loop, model router, providers, @tool registry and tools
  scheduler/        APScheduler wrapper, briefing and routine jobs, Watcher base + registry
  storage/          async SQLAlchemy models, database access, Alembic migrations
  packs/            domain pack loader, records, SKILL.md editing and change log
  radar/            Internship Radar: board adapters, extraction, matching, notify, CLI
  export.py         orbit export / import and the nightly snapshot
packs/              public example pack (habits)
persona/            system prompt and per-task prompts
tests/              pytest suite, recorded job-board fixtures in tests/fixtures/
evals/              labeled extraction eval set
deploy/             EC2 deploy script, production compose override, backups, runbook
scripts/            dev Postgres, Task Scheduler install, fixture recorder, privacy check
docs/               design decisions, pack guide, setup and radar reference
```

## Extending Orbit

| To add | Do this |
|---|---|
| A tool | Write a function in `orbit/core/tools/`, decorate it with `@tool`, import it in `orbit/core/tools/__init__.py`. |
| A watcher | Subclass `Watcher` (`orbit/scheduler/watchers/base.py`), register it with `@register_watcher("name")`, enable it under `watchers:` in `config.yaml`. |
| A domain pack | Add `packs/<name>/pack.yaml` and `SKILL.md`, list it in `packs.enabled`. No code. See [docs/PACKS.md](docs/PACKS.md). |
| A channel | Implement the `Channel` protocol in `orbit/channels/base.py` and register it in `orbit/app.py`. |
| A job board | Add an adapter in `orbit/radar/sources/` and follow the checklist in [docs/RADAR.md](docs/RADAR.md#adding-a-job-board). |
| A model or provider | Edit `config.yaml` only. |

## Testing

```bash
pip install -e ".[dev]"
export TEST_DATABASE_URL=postgresql+asyncpg://orbit:orbit@127.0.0.1:5432/orbit_test   # name must contain "test"
pytest
ruff check .
```

Tests run against a real Postgres because SQLite cannot test pgvector, JSONB or `ON CONFLICT`
(`docker compose up -d postgres` creates `orbit_test`). LLM, embedding and Telegram clients are fakes,
and the job-board adapters replay recorded API responses, so the suite never touches the network.
[CI](.github/workflows/ci.yml) runs ruff, `alembic upgrade head` on a clean database, pytest and
`docker compose config` on every push, with a `pgvector/pgvector:pg16` service container.

## Tech stack

Python 3.12 · asyncio · FastAPI · Typer · python-telegram-bot · APScheduler · SQLAlchemy 2 (async) ·
Alembic · PostgreSQL 16 + pgvector · Pydantic · Anthropic and OpenAI SDKs · httpx · Docker Compose ·
GitHub Actions · pytest · ruff

## Docs

- [docs/DECISIONS.md](docs/DECISIONS.md): what was chosen, why, and what happens when it fails.
- [docs/PACKS.md](docs/PACKS.md): how to write a domain pack.
- [LEARNING.md](LEARNING.md): a short explanation of each module, with interview questions.
- [docs/SETUP.md](docs/SETUP.md): environment variables, local and Docker setup, daily CLI use.
- [docs/RADAR.md](docs/RADAR.md): Internship Radar commands, settings and metrics.
- [deploy/RUNBOOK.md](deploy/RUNBOOK.md): EC2 deploy, update, backup and rollback (prepared, not executed).

## License

[MIT](LICENSE) © Xinyang Qiao
