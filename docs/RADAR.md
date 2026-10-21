# Internship Radar

The radar tracks public job postings from tech companies hiring in Canada, extracts structured fields
with an LLM, scores each posting against a resume with pgvector, and pushes new matches to Telegram
once and never twice. Design choices and their failure modes are in [DECISIONS.md](DECISIONS.md).

## Pipeline

The `internship_radar` watcher (enabled under `watchers:` in `config.yaml`, every 30 minutes, first run
60 s after start) runs five stages:

1. **poll**: fetch every active company's board through `PoliteClient` (rate limit and retries) from
   the Greenhouse Job Board API, the Lever Postings API or the Ashby Posting API, then upsert on
   `(source, external_id)`. A content hash decides whether a posting changed; postings missing from a
   successful fetch are closed and reopened if they come back. A failed fetch closes nothing.
2. **extract**: pending postings go to the `extract` task with a strict JSON schema generated from a
   Pydantic model. Validators normalize terms, locations and tech stacks; one retry feeds the
   validation error back. A keyword prefilter skips obvious senior and PhD-only roles with no LLM call.
   At most `max_extract_per_poll` postings per run.
3. **embed**: extracted internships are embedded with the `embed` task (1536 dimensions).
4. **score**: hard rules (open, internship, term overlap, location or remote in Canada, role type) run
   as SQL filters; survivors are ranked by cosine similarity to the resume embedding, blended 0.8/0.2
   with an optional goal set through the `set_radar_goal` tool.
5. **notify**: each match above `notify_threshold` is claimed with
   `INSERT ... ON CONFLICT (posting_id) DO NOTHING RETURNING` before it is pushed. Failed pushes retry
   up to 3 times; extra matches go into one digest message.

Each stage is isolated: a missing API key or a provider outage skips that stage without stopping
polling.

## Getting started

```bash
orbit radar seed                    # load the companies from orbit/radar/seeds.yaml into the DB
orbit radar verify-sources          # fetch every board live; exits 1 if fewer than 20 pass
# save your resume as plain text or Markdown at data/resume.md (git-ignored)
orbit radar resume                  # embed it as the radar profile (needs OPENAI_API_KEY)
orbit radar poll                    # fetch and sync all boards once (no API key needed)
orbit radar extract                 # LLM extraction of pending postings
orbit radar embed                   # embed extracted internships
orbit radar top 10                  # best open matches with scores and the threshold cut line
orbit radar stats                   # the metrics below
```

With the daemon running you can skip `poll`, `extract` and `embed`: the watcher runs them. In Docker,
prefix each command with `docker compose exec orbit`.

In chat, `search_postings` answers questions such as "any new AI co-ops in Toronto?" (semantic
ranking when an embedding provider is configured, keyword ranking otherwise).

**Calibrating the threshold.** After changing your resume or goal, run `orbit radar top 30`, find the
weakest posting you would still want pushed, try cut points with `orbit radar top 30 --threshold 0.45`,
and set `radar.notify_threshold` just below that score.

## Commands

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

`--source` takes `greenhouse`, `lever` or `ashby`.

## Configuration: the `radar:` section of `config.yaml`

| Key | Meaning |
|---|---|
| `terms` | Work terms you want, as `"<Season> <YYYY>"`. Postings whose terms are unstated still pass. |
| `locations` | A posting passes if an extracted location is listed, or it is remote and open to Canada. Format `"City, XX"` or `"Remote (Canada)"`. |
| `role_types` | Hard filter on the extracted role type; empty means any role. Unknown names fail at load. |
| `notify_threshold` | Minimum score that gets pushed to Telegram (see calibration above). |
| `resume_path` | Resume file, relative to `ORBIT_HOME` (default `data/resume.md`). |
| `max_extract_per_poll` | Caps LLM extraction cost per watcher run. |
| `max_notifications_per_poll` | Individual Telegram messages per run; the rest go into one digest message. |

The models come from the `extract` and `embed` tasks under `tasks:`; alternative `extract` routes are
left commented in `config.yaml`. Changing the embedding model or dimension needs a migration and a
re-embed.

## Metrics

`orbit radar stats` prints: companies tracked (by source), postings stored (by status and extraction
state), extraction accuracy per field from the last `orbit radar eval`, posting-to-notification
latency (median and p90 of `sent_at - first_seen_at`), and uptime days (days with at least one
successful poll). `--json` prints the same data as JSON.

## Adding a job board

1. Add an adapter in `orbit/radar/sources/` that returns `NormalizedPosting`s and register it in
   `sources/__init__.py`.
2. Add the source name to `RADAR_SOURCES` in `orbit/storage/models.py`, with an Alembic migration for
   its CHECK constraints.
3. Record a fixture with `scripts/record_fixture.py` and test the adapter against it.

Only public, unauthenticated job-board APIs are in scope; nothing behind a login is scraped.
