"""SKILL.md editing: replace one "## " section, never a locked one, keeping a copy of every old version.

Every edit is also appended to a change log (data/skill_changes.jsonl, one JSON object per line:
id, time, pack, section, reason, old, new), so the owner can see what Orbit changed and why, and revert
one change with `revert_change`. `old` is null when the edit added the section.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from orbit.packs import Pack

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_ANY_HEADING_RE = re.compile(r"^#{1,2}\s")
# A section body may start with a marker line ("🔒 锁定", "✏️ agent 可改"); a rewrite keeps it.
MARKERS = ("🔒", "✏️", "✏")
CHANGES_FILE = "skill_changes.jsonl"


def section_names(text: str) -> list[str]:
    return [m.group(1) for line in text.splitlines() if (m := _HEADING_RE.match(line))]


def replace_section(text: str, section: str, new_text: str) -> tuple[str, bool]:
    """Text with `section`'s body replaced (or a new section appended); second value: True if it existed."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if (m := _HEADING_RE.match(ln)) and m.group(1) == section), None)
    body = new_text.strip("\n").splitlines()
    if start is None:
        out = [*lines]
        while out and not out[-1].strip():
            out.pop()
        return "\n".join([*out, "", f"## {section}", *body]) + "\n", False
    end = next((i for i in range(start + 1, len(lines)) if _HEADING_RE.match(lines[i])), len(lines))
    marker = next((ln for ln in lines[start + 1 : end] if ln.strip()), "")
    first_new = next((ln for ln in body if ln.strip()), "")
    if marker.lstrip().startswith(MARKERS) and not first_new.lstrip().startswith(MARKERS):
        body = [marker, *body]
    tail = lines[end:]
    return "\n".join([*lines[: start + 1], *body, *([""] if tail else []), *tail]) + "\n", True


def _bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    start = next((i for i, ln in enumerate(lines) if (m := _HEADING_RE.match(ln)) and m.group(1) == section), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if _HEADING_RE.match(lines[i])), len(lines))
    return start, end


def section_body(text: str, section: str) -> str | None:
    """The body of a "## " section without surrounding blank lines; None if there is no such section."""
    lines = text.splitlines()
    bounds = _bounds(lines, section)
    if bounds is None:
        return None
    return "\n".join(lines[bounds[0] + 1 : bounds[1]]).strip("\n")


def remove_section(text: str, section: str) -> str:
    lines = text.splitlines()
    bounds = _bounds(lines, section)
    if bounds is None:
        return text
    out = [*lines[: bounds[0]], *lines[bounds[1] :]]
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def read_skill(pack: Pack) -> str:
    return pack.skill_path.read_text(encoding="utf-8")


def write_skill(
    pack: Pack,
    section: str,
    new_text: str,
    *,
    reason: str = "",
    log: Path | None = None,
    now: datetime | None = None,
) -> str:
    """Replace one section of the pack's SKILL.md and append the change to `log` (if given).

    Returns a one-line result; refusals start with "error:".
    """
    section = section.strip().removeprefix("##").strip()
    if not section:
        return "error: section is required"
    if section in pack.locked_sections:
        return f"error: section '{section}' is locked; only the user can edit it, by hand in {pack.skill_path.name}"
    if any(_ANY_HEADING_RE.match(line) for line in new_text.splitlines()):
        return "error: new_text must not contain '#' or '##' headings; write only the section body"
    old = read_skill(pack)
    updated, existed = replace_section(old, section, new_text)
    if updated == old:
        return f"section '{section}' unchanged"
    _save(pack, updated, now)
    if log is not None:
        _log_change(log, pack.name, section, reason, section_body(old, section), section_body(updated, section), now)
    return f"{'updated' if existed else 'added'} section '{section}' in {pack.name}/SKILL.md"


def _save(pack: Pack, updated: str, now: datetime | None) -> None:
    """Copy the current SKILL.md to .history/, then write the new text."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")
    pack.history_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pack.skill_path, pack.history_dir / f"SKILL.{stamp}.md")
    pack.skill_path.write_text(updated, encoding="utf-8", newline="\n")


# ---- change log ----------------------------------------------------------------------------------------


def read_changes(log: Path) -> list[dict[str, Any]]:
    """Every logged change, oldest first."""
    if not log.is_file():
        return []
    with open(log, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_changes(log: Path, entries: list[dict[str, Any]]) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8", newline="\n") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _log_change(
    log: Path,
    pack: str,
    section: str,
    reason: str,
    old: str | None,
    new: str | None,
    now: datetime | None,
    **extra: Any,
) -> dict[str, Any]:
    entry = {
        "id": max((int(c["id"]) for c in read_changes(log)), default=0) + 1,
        "time": (now or datetime.now()).replace(microsecond=0).isoformat(),
        "pack": pack,
        "section": section,
        "reason": reason.strip(),
        "old": old,
        "new": new,
        **extra,
    }
    append_changes(log, [entry])
    return entry


def list_changes(log: Path, *, since: datetime | None = None) -> list[dict[str, Any]]:
    """Logged changes, newest first; with `since`, only those at or after it."""
    changes = [c for c in read_changes(log) if since is None or datetime.fromisoformat(c["time"]) >= since]
    return changes[::-1]


def revert_change(packs: dict[str, Pack], log: Path, change_id: int, *, now: datetime | None = None) -> str:
    """Put a section back the way it was before change `change_id` (removing it if that change added it).

    Refused when the section is locked now, or when it changed again afterwards (revert the later change
    first), so a revert never silently throws away a newer edit. The revert is logged as a change too.
    """
    changes = read_changes(log)
    entry = next((c for c in changes if int(c["id"]) == change_id), None)
    if entry is None:
        return f"error: no skill change #{change_id}"
    pack = packs.get(entry["pack"])
    if pack is None:
        return f"error: pack '{entry['pack']}' is not loaded"
    section = entry["section"]
    if section in pack.locked_sections:
        return f"error: section '{section}' is locked; only the user can edit it, by hand in {pack.skill_path.name}"
    text = read_skill(pack)
    current = section_body(text, section)
    if current != entry["new"]:
        later = [
            f"#{c['id']}" for c in changes
            if int(c["id"]) > change_id and c["pack"] == pack.name and c["section"] == section
        ]
        if later:
            return f"error: section '{section}' changed again later ({', '.join(later)}); revert those first"
        return f"error: section '{section}' no longer matches change #{change_id} (edited by hand?); not reverted"
    if entry["old"] is None:
        updated = remove_section(text, section)
    else:
        updated, _ = replace_section(text, section, entry["old"])
    _save(pack, updated, now)
    _log_change(log, pack.name, section, f"revert #{change_id}", current, entry["old"], now, reverts=change_id)
    return f"reverted change #{change_id}: section '{section}' in {pack.name}/SKILL.md"
