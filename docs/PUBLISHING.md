# Publishing the public mirror

Orbit lives in two repositories:

| Repository | What it is | Who writes to it |
|---|---|---|
| `github.com/xy9iao/orbit` (private) | the only place Orbit is developed: branches, PRs, CI, issues | you and coding agents, as usual |
| `github.com/xy9iao/orbit-agent` (public) | a release-only mirror, frozen at tags | only `scripts/publish_public.sh`, run by you |

Never commit to, open PRs against, or develop in the public mirror. Its history is regenerated from the
private `main` at every release, so anything committed there directly is overwritten.

## What the mirror leaves out

`scripts/publish_public.sh` rewrites a fresh clone of the private `main` with `git filter-repo`, over
the whole history:

- **Paths**, removed from every commit: `private-packs/`, `packs/fitness/`, `packs/nutrition/`,
  `packs/study/` (they were in git before the private root existed), and every path in
  [`.publicignore`](../.publicignore) (today: `QUESTIONS.md` and `PROGRESS.md`, the agents' work log and
  to-do notes for the owner). Commits that only touched those paths disappear.
- **Terms**, replaced with `[redacted]` in file contents and commit messages: every line of
  `private-packs/.privacy-terms`. A term is replaced as a whole word only (not inside a longer ASCII
  word), the same rule `scripts/privacy_check.sh` uses, so code such as an environment variable name
  that merely contains a term is not broken.

Only what the terms file names is redacted. Words around a term (a description of the same detail in
other words) stay in old commits; when a dry run shows something you would not publish, add it to the
terms file or the path to `.publicignore` and run again.

## Releasing

```bash
# 1. In the private repo, on an up-to-date main with CI green:
git checkout main && git pull

# 2. Dry run: builds the cleaned snapshot, checks it, prints a summary, pushes nothing.
bash scripts/publish_public.sh v0.2.0 --dry-run
#    Look through the work dir it prints (git log -p, the file list) if anything is new.

# 3. Publish: same steps, then asks you to type "yes" before pushing.
bash scripts/publish_public.sh v0.2.0
```

The script:

1. clones `main` from the private origin into a temp dir (`--source` and `--workdir` override this);
2. runs `git filter-repo` with the removals and replacements above, and drops the clone's `origin`, so
   it can only push to the public remote;
3. runs `scripts/privacy_check.sh --history` on the result (every commit message, path and diff, the
   same terms) and checks that no removed path is left in any commit. Any match aborts, and the work
   dir is kept for inspection;
4. tags the rewritten `main` as `TAG` and prints a summary: commit count, file count, top-level
   entries, removed paths, remaining matches (always 0 when it gets this far);
5. without `--dry-run`: refuses a `TAG` that already exists on the mirror (it is frozen at its tags),
   asks for `yes`, then pushes `main` with `--force-with-lease` and the tag.

Requirements: `git-filter-repo` (`pip install -e ".[dev]"` installs it) and the terms file at
`private-packs/.privacy-terms` (`--terms FILE` to use another one). Without a terms file the script
refuses to run. `--remote URL` overrides the public remote (default
`https://github.com/xy9iao/orbit-agent.git`).

## Rules

- Develop only in the private repo; the mirror is never a source of changes.
- Publish only from a green `main`, and always with a new tag (`v0.2.0`, `v0.2.1`, ...).
- Run the dry run first, every time.
- Treat anything that ever reached the public mirror as published: deleting it later does not remove
  it from forks, caches or clones. If something slips through, rotate what can be rotated (keys,
  tokens) and republish with the term added.
- `tests/test_publish_public.py` runs the script in dry-run mode on a fixture repo in CI, so a change
  that stops the cleaning from working fails the build.
