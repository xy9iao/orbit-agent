# Writing a pack

A pack teaches Orbit one area of life (habits, workouts, meals, study time) with two files and no Python:

```
<root>/<name>/
  pack.yaml   what records look like, scheduled routines, which SKILL.md sections are locked
  SKILL.md    the rules Orbit follows in this area, one "## " section per topic
```

`packs/habits/` is a complete, small example. Copy it to start a new pack.

## Public and private packs

Orbit loads packs from two roots:

| Root | Where | In git | Use it for |
|---|---|---|---|
| public | `packs/` (`packs.dir` in `config.yaml`) | yes | examples anyone can read |
| private | `private-packs/` (env `ORBIT_PRIVATE_PACKS`; relative to `ORBIT_HOME`) | no (`.gitignore`) | your own packs: anything about your body, health, grades or routines |

- A private pack **overrides** a public one with the same name: copy `packs/habits` to
  `private-packs/habits` and edit it to make it yours without touching the example.
- Only names listed in `packs.enabled` load. A name found in neither root is logged and skipped, so a
  fresh clone without your `private-packs/` still starts.
- Everything works the same in both roots: `write_skill` edits the pack where it lives and keeps old
  versions in that pack's `.history/`, every edit goes to `data/skill_changes.jsonl`, and `orbit export`
  includes both roots. The export manifest records each pack's root, and `orbit import` restores a pack
  into the root it came from (into the private root if the export does not say).
- Docker: `docker-compose.yml` bind-mounts `./private-packs` like `./data`; it is never copied into the
  image (`.dockerignore`). On a server, copy the folder over yourself (`scp -r private-packs ...`): it
  is git-ignored, so `git pull` never brings it.

### Keeping personal details out of git

`scripts/privacy_check.sh` fails if any tracked file contains a term from `private-packs/.privacy-terms`
(one term per line, `#` comments allowed; literal, case-sensitive, whole-word). Put the things that must
never be published there: diagnoses, exact body numbers, grades.

```bash
bash scripts/privacy_check.sh             # check tracked files
bash scripts/privacy_check.sh --staged    # check what is about to be committed
git config core.hooksPath scripts/git-hooks   # once per clone: run it as a pre-commit hook
```

CI runs it only when the terms file exists; set the repository secret `PRIVACY_TERMS` (one term per
line) to turn it on there. The check looks at current files, not git history: a detail that was
committed once stays in history until the history is rewritten.

## pack.yaml

```yaml
name: habits                 # must equal the folder name
description: >-              # shown in every chat prompt; say when Orbit should use this pack
  Daily habit tracker. Use when the user mentions a habit, a check-in or a streak.
schema_version: 1            # bump when a collection schema changes; stored on every record
collections:                 # name -> JSON Schema (Draft 2020-12) of one record's data
  checkins:
    type: object
    required: [habit, done]
    properties:
      habit: {type: string, minLength: 1}
      done: {type: boolean}
      note: {type: string}
routines:                    # optional; run through the full agent loop, reply pushed to Telegram
  - id: evening_checkin      # for you; Orbit ignores it
    cron: "0 21 * * *"       # standard crontab, local time: 0 and 7 are Sunday, 1,3 is Mon and Wed
    prompt: ...              # reply exactly NO_MESSAGE when there is nothing to send today
locked_sections: [Habits]    # SKILL.md "## " headings write_skill must refuse
```

- Schemas are enforced on `add_record` and `update_record`, including `format: date`. Every
  violation goes back to the model as `path: message` so it can fix the call.
- Keep `additionalProperties` open unless you need it closed: the model may add a `photo` path or a note.
- All packs share one `records` table, so a new collection is a YAML change, not a migration. Records
  are never rewritten when a schema changes; `schema_version` says which schema a row was valid against.
- Routines get the pack's SKILL.md in the system prompt. Name the tools in the prompt
  (`query_records`, `record_stats`) and the sections to follow, and keep the reply short.

## SKILL.md

- One `## ` section per topic. The chat prompt lists only each pack's name and description; Orbit
  reads the whole SKILL.md with `load_skill` before working in the area.
- Put a marker line under each heading: `🔒 locked` for sections only you edit, `✏️ agent may edit`
  for sections Orbit may rewrite when you correct it. `write_skill` keeps the marker line.
- Lock the sections that hold goals, limits and safety rules (list them in `locked_sections`); a typo
  in that list is logged as a warning, because it would leave the real section editable.
- Give a record-format section with one or two examples of what you say and the record it becomes.
  That is what makes logging from chat reliable.
- Orbit's edits: `list_skill_changes` shows what it changed and why, `revert_skill_change(id)` undoes
  one. A revert is refused when the section changed again later or is locked.

## Checklist

1. Create `private-packs/<name>/` (or `packs/<name>/` for a public example) with both files.
2. Add the name to `packs.enabled` in `config.yaml`.
3. Restart Orbit (`docker compose restart orbit`) and check the log line `packs loaded: ...`.
4. Tell Orbit something to log and look at the record with `query_records` (or ask Orbit).
