"""Plain-file snapshots of everything that is the user's (`orbit export` / `orbit import`, nightly job).

Layout of a snapshot folder (readable with any JSON/Markdown tool, no Orbit code needed):

    README.md                          what each file is
    manifest.json                      export time, Orbit version and git commit, row counts, schemas
    records/<pack>/<collection>.jsonl  one record per line: id, occurred_at, created_at, schema_version, data
    skills/<pack>/SKILL.md             each pack's skill file
    packs/<pack>/pack.yaml             each pack's definition (the JSON Schemas of its collections)
    skill_changes.jsonl                every write_skill edit: id, time, pack, section, reason, old, new
    memory/MEMORY.md                   long-term memory
    persona/                           system prompt and job skills
    reminders.jsonl, messages.jsonl    reminders and the chat history
    media/YYYY-MM-DD/...               photos received over Telegram (records reference data/media/...)

Import restores records (same ids), skills, packs that are missing locally, the skill change log, memory
and media. It is idempotent: rows and change-log entries whose id already exists, memory lines already
present and files already on disk are skipped.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url

from orbit import __version__
from orbit.config import Config, Settings
from orbit.packs import PACK_FILE, SKILL_FILE, skills
from orbit.storage.db import Database
from orbit.storage.models import Message, Record, Reminder

SNAPSHOT_PREFIX = "orbit-"
DUMP_SUFFIX = ".dump"

README = """# Orbit data export

A plain-file snapshot of the data Orbit keeps for its owner. Every file is JSON Lines, JSON, YAML or
Markdown, so it can be read without Orbit.

- `manifest.json`: when it was exported, from which Orbit version and git commit, how many rows each
  file holds, and the JSON Schema (with its `schema_version`) of every collection.
- `records/<pack>/<collection>.jsonl`: one record per line. `data` is the record as stored, valid against
  the collection schema of its `schema_version`; `occurred_at` is local time, ISO 8601.
- `packs/<pack>/pack.yaml`: the pack definitions (collections, routines, locked skill sections).
- `skills/<pack>/SKILL.md`: the instructions Orbit follows for each pack.
- `skill_changes.jsonl`: every edit Orbit made to a skill file (id, time, pack, section, reason, and the
  section's old and new text; old is null when the edit added the section).
- `memory/MEMORY.md`: long-term memory, one fact per line.
- `persona/`: Orbit's system prompt and job skills.
- `reminders.jsonl`, `messages.jsonl`: reminders and the conversation history.
- `media/`: photos sent to Orbit, by day. Records point at them as `data/media/<day>/<file>`.

Restore into Orbit with `orbit import <this folder>`.
"""


def git_commit(home: Path) -> str:
    """ORBIT_GIT_COMMIT (baked into the Docker image), else `git rev-parse HEAD` in ORBIT_HOME, else unknown."""
    if os.environ.get("ORBIT_GIT_COMMIT"):
        return os.environ["ORBIT_GIT_COMMIT"]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=home, capture_output=True, text=True, timeout=5, check=True
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _iso(value: Any) -> Any:
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else value


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps({k: _iso(v) for k, v in row.items()}, ensure_ascii=False) + "\n")
    return len(rows)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


async def _table_rows(db: Database, model: Any) -> list[dict[str, Any]]:
    async with db.engine.connect() as conn:
        rows = (await conn.execute(select(model.__table__).order_by(model.id))).mappings().all()
    return [dict(r) for r in rows]


def _pack_dirs(packs_dir: Path) -> list[Path]:
    if not packs_dir.is_dir():
        return []
    return sorted(p for p in packs_dir.iterdir() if (p / PACK_FILE).is_file())


async def export_snapshot(db: Database, settings: Settings, config: Config, out: Path) -> dict[str, Any]:
    """Write a full snapshot into `out` (created; existing files are overwritten). Returns the manifest."""
    out.mkdir(parents=True, exist_ok=True)
    packs_dir = settings.packs_path(config.packs)
    counts: dict[str, Any] = {"records": {}}

    records = await _table_rows(db, Record)
    by_collection: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in records:
        by_collection.setdefault((r["pack"], r["collection"]), []).append(
            {k: r[k] for k in ("id", "occurred_at", "created_at", "schema_version", "data")}
        )
    for (pack, collection), rows in sorted(by_collection.items()):
        n = _write_jsonl(out / "records" / pack / f"{collection}.jsonl", rows)
        counts["records"][f"{pack}/{collection}"] = n

    schemas: dict[str, Any] = {}
    for pack_dir in _pack_dirs(packs_dir):
        (out / "packs" / pack_dir.name).mkdir(parents=True, exist_ok=True)
        shutil.copy2(pack_dir / PACK_FILE, out / "packs" / pack_dir.name / PACK_FILE)
        if (pack_dir / SKILL_FILE).is_file():
            (out / "skills" / pack_dir.name).mkdir(parents=True, exist_ok=True)
            shutil.copy2(pack_dir / SKILL_FILE, out / "skills" / pack_dir.name / SKILL_FILE)
        try:
            raw = yaml.safe_load((pack_dir / PACK_FILE).read_text(encoding="utf-8")) or {}
            schemas[pack_dir.name] = {
                "schema_version": raw.get("schema_version", 1),
                "collections": raw.get("collections") or {},
            }
        except yaml.YAMLError as e:
            schemas[pack_dir.name] = {"error": f"pack.yaml is not valid YAML: {e}"}

    changes = skills.read_changes(settings.skill_changes_path)
    counts["skill_changes"] = len(changes)
    if changes:
        shutil.copy2(settings.skill_changes_path, out / skills.CHANGES_FILE)

    memory_file = settings.memory_dir / "MEMORY.md"
    if memory_file.is_file():
        (out / "memory").mkdir(exist_ok=True)
        shutil.copy2(memory_file, out / "memory" / "MEMORY.md")
    if settings.persona_dir.is_dir():
        shutil.copytree(settings.persona_dir, out / "persona", dirs_exist_ok=True)

    counts["reminders"] = _write_jsonl(out / "reminders.jsonl", await _table_rows(db, Reminder))
    counts["messages"] = _write_jsonl(out / "messages.jsonl", await _table_rows(db, Message))

    media_files = 0
    if settings.media_dir.is_dir():
        shutil.copytree(settings.media_dir, out / "media", dirs_exist_ok=True)
        media_files = sum(1 for p in (out / "media").rglob("*") if p.is_file())
    counts["media_files"] = media_files

    manifest = {
        "format": "orbit-export/1",
        "exported_at": datetime.now().replace(microsecond=0).isoformat(),
        "orbit_version": __version__,
        "git_commit": git_commit(settings.orbit_home),
        "counts": counts,
        "schemas": schemas,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "README.md").write_text(README, encoding="utf-8")
    return manifest


def _parse_dt(value: Any) -> Any:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _copy_missing(src: Path, dst: Path) -> int:
    copied = 0
    for path in src.rglob("*"):
        target = dst / path.relative_to(src)
        if path.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
    return copied


async def import_snapshot(db: Database, settings: Settings, config: Config, src: Path) -> dict[str, int]:
    """Restore records, skills, missing packs, memory and media from a snapshot. Idempotent."""
    if not (src / "manifest.json").is_file():
        raise FileNotFoundError(f"{src} is not an Orbit export (no manifest.json)")
    result = {
        "records": 0, "records_skipped": 0, "skills": 0, "packs": 0, "skill_changes": 0, "memory_lines": 0,
        "media_files": 0,
    }

    rows: list[dict[str, Any]] = []
    for path in sorted((src / "records").glob("*/*.jsonl")):
        for r in _read_jsonl(path):
            rows.append(
                {
                    "id": r["id"],
                    "pack": path.parent.name,
                    "collection": path.stem,
                    "occurred_at": _parse_dt(r["occurred_at"]),
                    "created_at": _parse_dt(r.get("created_at") or r["occurred_at"]),
                    "schema_version": r.get("schema_version", 1),
                    "data": r["data"],
                }
            )
    if rows:
        async with db.engine.begin() as conn:
            inserted = await conn.execute(
                pg_insert(Record).values(rows).on_conflict_do_nothing(index_elements=["id"]).returning(Record.id)
            )
            result["records"] = len(inserted.all())
            # Rows kept their ids, so move the sequence past them for new records.
            await conn.execute(
                select(func.setval(text("pg_get_serial_sequence('records', 'id')"), func.max(Record.id)))
            )
    result["records_skipped"] = len(rows) - result["records"]

    packs_dir = settings.packs_path(config.packs)
    for pack_file in sorted((src / "packs").glob(f"*/{PACK_FILE}")):
        target = packs_dir / pack_file.parent.name / PACK_FILE
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pack_file, target)
            result["packs"] += 1
    for skill_file in sorted((src / "skills").glob(f"*/{SKILL_FILE}")):
        target = packs_dir / skill_file.parent.name / SKILL_FILE
        new = skill_file.read_text(encoding="utf-8")
        if target.exists() and target.read_text(encoding="utf-8") == new:
            continue
        if target.exists():  # keep the local version, like write_skill does
            history = target.parent / ".history"
            history.mkdir(exist_ok=True)
            shutil.copy2(target, history / f"SKILL.{datetime.now():%Y%m%d-%H%M%S-%f}.before-import.md")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new, encoding="utf-8", newline="\n")
        result["skills"] += 1

    have = {int(c["id"]) for c in skills.read_changes(settings.skill_changes_path)}
    missing_changes = [c for c in skills.read_changes(src / skills.CHANGES_FILE) if int(c["id"]) not in have]
    if missing_changes:
        skills.append_changes(settings.skill_changes_path, missing_changes)
    result["skill_changes"] = len(missing_changes)

    memory_src = src / "memory" / "MEMORY.md"
    if memory_src.is_file():
        settings.memory_dir.mkdir(parents=True, exist_ok=True)
        memory_dst = settings.memory_dir / "MEMORY.md"
        if not memory_dst.exists():
            shutil.copy2(memory_src, memory_dst)
            result["memory_lines"] = sum(
                1 for ln in memory_src.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")
            )
        else:
            present = set(memory_dst.read_text(encoding="utf-8").splitlines())
            missing = [
                ln for ln in memory_src.read_text(encoding="utf-8").splitlines()
                if ln.startswith("- ") and ln not in present
            ]
            if missing:
                with open(memory_dst, "a", encoding="utf-8") as f:
                    f.write("".join(f"{ln}\n" for ln in missing))
            result["memory_lines"] = len(missing)

    if (src / "media").is_dir():
        result["media_files"] = _copy_missing(src / "media", settings.media_dir)
    return result


# ---- nightly job ---------------------------------------------------------------------------------------


async def pg_dump(database_url: str, path: Path) -> bool:
    """Custom-format pg_dump of the database to `path`; False (logged) if pg_dump is missing or fails."""
    exe = shutil.which("pg_dump")
    if exe is None:
        logger.warning("export: pg_dump not found on PATH - skipping the database dump")
        return False
    url = make_url(database_url)
    env = {**os.environ, "PGPASSWORD": url.password or ""}
    args = [exe, "--format=custom", f"--file={path}", f"--host={url.host or 'localhost'}"]
    args += [f"--port={url.port or 5432}", f"--username={url.username or 'postgres'}", f"--dbname={url.database}"]
    proc = await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        logger.error("export: pg_dump failed ({}): {}", proc.returncode, err.decode(errors="replace").strip()[:500])
        path.unlink(missing_ok=True)
        return False
    return True


def prune(export_dir: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` dated snapshots and dumps; returns what was removed."""
    removed: list[Path] = []
    snapshots = sorted(p for p in export_dir.glob(f"{SNAPSHOT_PREFIX}*") if p.is_dir())
    dumps = sorted(p for p in export_dir.glob(f"{SNAPSHOT_PREFIX}*{DUMP_SUFFIX}") if p.is_file())
    for path in snapshots[:-keep] if len(snapshots) > keep else []:
        shutil.rmtree(path)
        removed.append(path)
    for path in dumps[:-keep] if len(dumps) > keep else []:
        path.unlink()
        removed.append(path)
    return removed


async def nightly_export(
    db: Database, settings: Settings, config: Config, *, now: datetime | None = None
) -> dict[str, Any]:
    """EXPORT_DIR/orbit-YYYY-MM-DD/ snapshot + orbit-YYYY-MM-DD.dump next to it, then keep the newest N."""
    export_dir = settings.exports_dir
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{SNAPSHOT_PREFIX}{(now or datetime.now()):%Y-%m-%d}"
    snapshot = export_dir / stamp
    if snapshot.exists():
        shutil.rmtree(snapshot)  # a rerun the same day replaces that day's snapshot
    manifest = await export_snapshot(db, settings, config, snapshot)
    dumped = await pg_dump(settings.database_url, export_dir / f"{stamp}{DUMP_SUFFIX}")
    removed = prune(export_dir, config.jobs.export.keep)
    logger.info(
        "export: wrote {} (records {}, messages {}), pg_dump {}, pruned {}",
        snapshot,
        sum(manifest["counts"]["records"].values()),
        manifest["counts"]["messages"],
        "ok" if dumped else "skipped",
        len(removed),
    )
    return {"snapshot": snapshot, "dumped": dumped, "removed": removed, "manifest": manifest}
