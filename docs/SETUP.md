# Setup and daily use

The [README quickstart](../README.md#quickstart) gets Orbit running in Docker. This page covers the
rest: every environment variable, a local (non-Docker) setup, Windows Task Scheduler, EC2 and the CLI.

## Environment

Secrets live only in `.env` (git-ignored); copy `.env.example` and fill in:

| Variable | Needed for |
|---|---|
| `ANTHROPIC_API_KEY` | the `chat`, `briefing`, `code`, `fast`, `summarize` and `extract` tasks (Claude) |
| `OPENAI_API_KEY` | the `embed` task (text-embedding-3-small) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CHAT_ID` | the Telegram channel and all pushes. Without them Orbit runs CLI-only. |
| `DATABASE_URL` | the Postgres URL (Compose sets its own for the container) |
| `TEST_DATABASE_URL` | pytest only |
| `PROXY_URL` (optional) | an HTTP proxy for OpenAI and Telegram |

**Telegram bot:** create a bot with [@BotFather](https://t.me/BotFather) to get the token, and get your
chat id from @userinfobot. Orbit only answers the owner chat id; if it is wrong, send the bot `/id` and
it replies with the id to put in `.env`.

Migrations run automatically: the container runs `alembic upgrade head` before `orbit serve`, and
`orbit serve` migrates on start too.

## Docker Compose

```bash
cp .env.example .env
docker compose up -d --build    # postgres (pgvector) + orbit
curl http://127.0.0.1:8765/health
docker compose logs -f orbit
```

Both ports are published on 127.0.0.1 only (`POSTGRES_PORT`, `ORBIT_PORT` override them). `memory/`,
`persona/`, `data/`, `logs/`, `packs/` and `private-packs/` are mounted from the repo, so for example
`data/resume.md` on the host is visible to the container. Run any CLI command inside the container
with `docker compose exec orbit orbit <command>`.

> **PROXY_URL inside Docker:** `127.0.0.1` in the container is the container itself. Use
> `http://host.docker.internal:<port>` in Docker, and leave it unset on EC2.

## Local, with Windows Task Scheduler

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env                  # DATABASE_URL already points at 127.0.0.1:5432
docker compose up -d postgres           # just the database
.venv\Scripts\orbit serve               # run the daemon in the foreground (Ctrl+C to stop)
```

No Docker? `pip install pgserver` and run `python scripts\dev_postgres.py`: it starts a local Postgres
with pgvector in `data/pgserver` and creates the `orbit` and `orbit_test` databases
(`python scripts\dev_postgres.py --stop` stops it).

To keep Orbit running in the background, register it as a scheduled task (from an elevated
PowerShell). It starts at logon and restarts one minute after a crash:

```powershell
.\scripts\install_task.ps1
.\scripts\uninstall_task.ps1            # remove it
```

Logs go to `logs/orbit.log`.

**Upgrading from the SQLite version:** `python scripts\migrate_sqlite_to_pg.py` copies an old
`data/orbit.db` (messages, reminders, usage, kv) into `DATABASE_URL` once. It keeps ids and is safe to
re-run.

## AWS EC2

[`deploy/RUNBOOK.md`](../deploy/RUNBOOK.md) covers instance sizing, the security group (SSH only), the
first deploy with `deploy/ec2_deploy.sh`, updates, rollback, backups (`deploy/backup.sh`) and secret
rotation. These scripts are prepared, not executed.

## Daily use

```bash
orbit chat                      # interactive chat (shares memory with Telegram); /clear, /quit
orbit send "remind me to call home in 10 minutes"
orbit status                    # uptime, channels, model routes, scheduled jobs, today's token usage
orbit reminders                 # pending reminders; `orbit cancel <id>` cancels one
orbit jobs                      # scheduled jobs and their next run
orbit briefing                  # generate and push today's briefing now
orbit push "hello"              # push raw text to Telegram, no model
orbit export [--out DIR]        # plain-file snapshot (JSONL, Markdown, YAML) + README.md + manifest.json
orbit import DIR                # restore records, skills, memory and media; rows already present are skipped
```

These talk to the running daemon over the local API, except `export` and `import`, which work without
it. `orbit radar ...` commands do not need the daemon either (see [RADAR.md](RADAR.md)).

**Incognito.** For questions that should leave no trace, send `/incognito` (in Telegram or
`orbit chat`) or start a single message with `??`. Replies then start with 🕶; nothing goes into the
chat history, memory, skills or records, photos are not saved, and logs show only message lengths.
`/incognito off` ends it, and 30 minutes of silence ends it too.

**Nightly export.** Every night at 03:00 Orbit writes `orbit-YYYY-MM-DD/` plus a `pg_dump` into
`EXPORT_DIR` and keeps the newest 14. In Docker that folder is bind-mounted to `./exports` on the host
(`EXPORT_HOST_DIR` changes it). A snapshot needs no Orbit code to read; see its README.md.

## Configuration files

- `.env`: secrets and machine-specific values (never committed).
- `config.yaml`: providers, the task to model table, agent limits, the local API address, jobs,
  watchers, packs and the `radar:` section. A provider is `type: anthropic` (the Anthropic Messages
  API) or `type: openai` (the default): any OpenAI-compatible endpoint works by adding a provider with
  its `base_url`.
- `persona/system.md`: Orbit's system prompt. `persona/skills/*.md`: extra prompts per task (for
  example the briefing).
- `memory/MEMORY.md`: long-term memory that Orbit maintains itself through the `remember` and `forget`
  tools (git-ignored).
- `packs/` and `private-packs/`: domain packs, see [PACKS.md](PACKS.md).
