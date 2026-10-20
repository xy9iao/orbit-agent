"""Domain packs: a folder per life domain (habits, fitness, ...) that Orbit loads at startup.

Packs live in two roots: packs/ (in git, public examples) and a private root (ORBIT_PRIVATE_PACKS,
default private-packs/, git-ignored). A private pack overrides a public one with the same name.

<root>/<name>/pack.yaml
    name, description, schema_version
    collections: {<collection>: <JSON Schema for one record's data>}
    routines:    [{cron, prompt}]   run through the agent with the pack's skill, reply pushed to Telegram
    locked_sections: [<SKILL.md "## " heading>]   write_skill refuses these
<root>/<name>/SKILL.md
    the pack's instructions, one "## " section per topic; loaded on demand with load_skill

A pack that fails to load (missing file, bad YAML, invalid schema or cron) is logged and skipped; the
others still load.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from loguru import logger

from orbit.cron import cron_trigger

PACK_FILE = "pack.yaml"
SKILL_FILE = "SKILL.md"
HISTORY_DIR = ".history"


@dataclass(frozen=True)
class Routine:
    cron: str  # 5-field crontab; use day names (mon-sun): APScheduler numbers weekdays from Monday = 0
    prompt: str


@dataclass
class Pack:
    name: str
    description: str
    dir: Path
    schema_version: int = 1
    collections: dict[str, dict[str, Any]] = field(default_factory=dict)
    routines: list[Routine] = field(default_factory=list)
    locked_sections: list[str] = field(default_factory=list)

    @property
    def skill_path(self) -> Path:
        return self.dir / SKILL_FILE

    @property
    def history_dir(self) -> Path:
        return self.dir / HISTORY_DIR

    def validator(self, collection: str) -> Draft202012Validator:
        if collection not in self.collections:
            known = ", ".join(self.collections) or "none"
            raise KeyError(f"pack '{self.name}' has no collection '{collection}' (collections: {known})")
        # format_checker: a schema's "format: date" is enforced, not just documented
        schema = self.collections[collection]
        return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


class PackError(ValueError):
    pass


def load_pack(pack_dir: Path) -> Pack:
    """Read and validate one pack folder; raises PackError with a readable reason."""
    pack_file = pack_dir / PACK_FILE
    if not pack_file.is_file():
        raise PackError(f"{pack_file} not found")
    try:
        raw = yaml.safe_load(pack_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise PackError(f"{pack_file}: invalid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise PackError(f"{pack_file}: expected a mapping at the top level")

    name = str(raw.get("name") or "").strip()
    if name != pack_dir.name:
        raise PackError(f"{pack_file}: name {name!r} must match the folder name {pack_dir.name!r}")
    description = str(raw.get("description") or "").strip()
    if not description:
        raise PackError(f"{pack_file}: description is required")
    try:
        schema_version = int(raw.get("schema_version", 1))
    except (TypeError, ValueError):
        raise PackError(f"{pack_file}: schema_version must be an integer") from None

    collections = raw.get("collections") or {}
    if not isinstance(collections, dict):
        raise PackError(f"{pack_file}: collections must map a name to a JSON Schema")
    for cname, schema in collections.items():
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as e:  # jsonschema.SchemaError, or a non-dict schema
            raise PackError(f"{pack_file}: collection {cname!r} has an invalid schema: {_first_line(e)}") from None

    routines: list[Routine] = []
    for i, item in enumerate(raw.get("routines") or []):
        if not isinstance(item, dict) or not item.get("cron") or not item.get("prompt"):
            raise PackError(f"{pack_file}: routines[{i}] needs cron and prompt")
        try:
            cron_trigger(str(item["cron"]))
        except ValueError as e:
            raise PackError(f"{pack_file}: routines[{i}] cron {item['cron']!r}: {e}") from None
        routines.append(Routine(cron=str(item["cron"]), prompt=str(item["prompt"]).strip()))

    if not (pack_dir / SKILL_FILE).is_file():
        raise PackError(f"{pack_dir / SKILL_FILE} not found")
    locked = [str(s).strip() for s in raw.get("locked_sections") or []]
    headings = {
        line[3:].strip() for line in (pack_dir / SKILL_FILE).read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    }
    for section in locked:
        if section not in headings:  # a typo would leave the real section editable
            logger.warning("pack '{}': locked section '{}' is not a '## ' heading in SKILL.md", name, section)
    return Pack(
        name=name,
        description=description,
        dir=pack_dir,
        schema_version=schema_version,
        collections={str(k): v for k, v in collections.items()},
        routines=routines,
        locked_sections=locked,
    )


def _roots(roots: Path | list[Path]) -> list[Path]:
    return [roots] if isinstance(roots, Path) else list(roots)


def pack_dirs(roots: Path | list[Path]) -> dict[str, Path]:
    """Every pack folder (one with a pack.yaml) by name; a later root wins over an earlier one."""
    found: dict[str, Path] = {}
    for root in _roots(roots):
        if root.is_dir():
            found.update({p.name: p for p in sorted(root.iterdir()) if (p / PACK_FILE).is_file()})
    return dict(sorted(found.items()))


def load_packs(roots: Path | list[Path], enabled: list[str]) -> dict[str, Pack]:
    """Load every enabled pack from the roots (lowest precedence first: public, then private).

    A broken or missing pack logs an error and is skipped.
    """
    roots = _roots(roots)
    packs: dict[str, Pack] = {}
    for name in enabled:
        candidates = [root / name for root in roots if (root / name / PACK_FILE).is_file()]
        if not candidates:
            logger.error("pack '{}' not found in {} and is skipped", name, " or ".join(str(r) for r in roots))
            continue
        if len(candidates) > 1:
            logger.info("pack '{}': {} overrides {}", name, candidates[-1], candidates[0])
        try:
            packs[name] = load_pack(candidates[-1])
        except Exception as e:  # noqa: BLE001 - one broken pack must not stop Orbit
            logger.error("pack '{}' failed to load and is skipped: {}", name, _first_line(e))
    if packs:
        summary = (f"{p.name} ({len(p.collections)} collections, {len(p.routines)} routines)" for p in packs.values())
        logger.info("packs loaded: {}", ", ".join(summary))
    return packs


def _first_line(e: BaseException) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0]
