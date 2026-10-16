# Orbit: notes for coding agents

Orbit is my (Xinyang, Waterloo CS 3A) always-on personal agent: Telegram and CLI channels, a model router (config.yaml maps tasks to provider/model), a tool registry (@tool), APScheduler jobs, and a Watcher plugin interface. It is a resume project.

## Current goal: Internship Radar
Orbit's first real vertical. A watcher tracks public job postings from Canadian tech companies, extracts structured fields with an LLM, scores each posting against my resume, and pushes new high-scoring matches to me on Telegram. I can also ask Orbit about postings in chat through tools. Later: 👍/👎 feedback re-ranking, company briefs, a public Discord feed, an MCP server exposing the radar tools.

## Fit the existing codebase
- New code lives in orbit/radar/ (sources/ with one adapter per job board, extract.py, match.py, models.py, cli.py). Polling runs through a Watcher registered with @register_watcher("internship_radar") and enabled in config.yaml. Chat access goes through @tool functions in orbit/core/tools/.
- LLM and embedding calls go through orbit/core/router.py using new task names in config.yaml (for example extract and embed). Never call providers directly.
- Keep existing behavior and tests passing. Do not rewrite modules the current task does not need.

## Stack (fixed)
- Storage: PostgreSQL + pgvector via SQLAlchemy 2 (async) + Alembic, for all of Orbit (messages, reminders, usage, kv and the radar tables). Every schema change is an Alembic migration.
- Docker Compose runs orbit and postgres. Deploy target: one AWS EC2 instance (scripts prepared, never run by agents). The Windows Task Scheduler script stays as the local option.
- Scheduling stays in-process with APScheduler. No Redis or Celery in the MVP.
- pytest + ruff. GitHub Actions runs both on every push with a Postgres service container.

## Rules
- Only public job-board APIs: Greenhouse Job Board API, Lever Postings API, Ashby Posting API. Never scrape WaterlooWorks, LinkedIn, or anything behind a login. Respect rate limits.
- Idempotent polling: dedupe on (source, external_id), mark postings closed when they disappear, never notify the same posting twice.
- Secrets only from environment variables or a git-ignored .env. Tests use fake LLM, embedding and channel clients and recorded API fixtures, never the live network.
- Keep docs/DECISIONS.md (what we chose, why, what happens when it fails) and LEARNING.md (per module: a 5-line explanation and 2 interview questions with short answers).
- Small commits with clear messages. Nothing outside PLAN.md.
- Parallelism: when two or more unchecked tasks have all dependencies done and touch different files, run them in parallel with subagents, each in its own git worktree. Merge them back one at a time and run the full test suite after each merge. Never run two tasks in parallel that both create Alembic migrations or both edit config.yaml.

## Metrics (`orbit radar stats` prints them)
Companies tracked, postings stored, extraction accuracy per field, posting-to-notification latency, uptime days.
