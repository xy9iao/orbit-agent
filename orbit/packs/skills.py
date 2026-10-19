"""SKILL.md editing: replace one "## " section, never a locked one, keeping a copy of every old version."""

from __future__ import annotations

import re
import shutil
from datetime import datetime

from orbit.packs import Pack

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_ANY_HEADING_RE = re.compile(r"^#{1,2}\s")
# A section body may start with a marker line ("🔒 锁定", "✏️ agent 可改"); a rewrite keeps it.
MARKERS = ("🔒", "✏️", "✏")


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


def read_skill(pack: Pack) -> str:
    return pack.skill_path.read_text(encoding="utf-8")


def write_skill(pack: Pack, section: str, new_text: str, *, now: datetime | None = None) -> str:
    """Replace one section of the pack's SKILL.md. Returns a one-line result; refusals start with "error:"."""
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
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")
    pack.history_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pack.skill_path, pack.history_dir / f"SKILL.{stamp}.md")
    pack.skill_path.write_text(updated, encoding="utf-8", newline="\n")
    return f"{'updated' if existed else 'added'} section '{section}' in {pack.name}/SKILL.md"
