"""Long-term memory as a plain markdown file the agent reads and appends to."""

from __future__ import annotations

from datetime import date
from pathlib import Path

HEADER = "# Orbit Memory\n\n长期记忆：用户的习惯、项目、人物、偏好和决定。一行一条。\n\n"


class MemoryStore:
    def __init__(self, memory_dir: Path):
        self.dir = memory_dir
        self.file = memory_dir / "MEMORY.md"

    def ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.file.exists():
            self.file.write_text(HEADER, encoding="utf-8")

    def read(self) -> str:
        self.ensure()
        return self.file.read_text(encoding="utf-8")

    def entries(self) -> list[str]:
        return [ln for ln in self.read().splitlines() if ln.startswith("- ")]

    def remember(self, note: str) -> str:
        note = " ".join(note.split())
        if any(note in ln for ln in self.entries()):
            return "already remembered"
        line = f"- [{date.today().isoformat()}] {note}"
        with open(self.file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return f"remembered: {note}"

    def forget(self, needle: str) -> int:
        lines = self.read().splitlines()
        kept = [ln for ln in lines if not (ln.startswith("- ") and needle in ln)]
        removed = len(lines) - len(kept)
        if removed:
            self.file.write_text("\n".join(kept) + "\n", encoding="utf-8")
        return removed
