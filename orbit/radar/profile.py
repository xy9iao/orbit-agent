"""Resume and goal profile: one embedded row per kind in `radar_profile`.

The resume is a plain-text or Markdown file (PLAN.md Decisions: "Resume input format"); the goal is
free text from chat. Each is re-embedded only when its sha256 `text_hash` changes, so running
`orbit radar resume` twice costs one embedding call, not two.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orbit.core.router import ModelRouter
from orbit.storage.db import Database
from orbit.storage.models import EMBEDDING_DIM, PROFILE_KINDS, Profile

# Relative to ORBIT_HOME (Settings().orbit_home). data/ is git-ignored, so the resume never lands in git.
DEFAULT_RESUME_PATH = "data/resume.md"
RESUME_SUFFIXES = (".md", ".txt")
EMBED_TASK = "embed"
# text-embedding-3-small accepts 8191 tokens. English prose runs about 4 characters per token, so
# 24k characters (~6k tokens) stays well under the limit. Only the embedder input is truncated; the
# full text is stored, and a real one-to-two-page resume is far below this anyway.
MAX_EMBED_CHARS = 24_000


@dataclass(frozen=True)
class ProfileResult:
    kind: str
    changed: bool  # True when the text was (re-)embedded and the row written
    chars: int
    text_hash: str
    truncated: bool  # True when only the first MAX_EMBED_CHARS characters were embedded


def normalize_text(text: str) -> str:
    """Unify line endings and strip, so the same resume saved on Windows or Unix hashes the same."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


async def set_profile(db: Database, router: ModelRouter, kind: str, text: str) -> ProfileResult:
    """Upsert the `kind` profile row, embedding the text only if it changed (or was never embedded)."""
    if kind not in PROFILE_KINDS:
        raise ValueError(f"profile kind must be one of {PROFILE_KINDS}, got {kind!r}")
    text = normalize_text(text)
    if not text:
        raise ValueError(f"{kind} text is empty")
    digest = text_hash(text)
    truncated = len(text) > MAX_EMBED_CHARS

    async with db.session() as s:
        existing = (
            await s.execute(
                select(Profile.text_hash, Profile.embedding.is_not(None).label("embedded")).where(
                    Profile.kind == kind
                )
            )
        ).first()
    if existing is not None and existing.text_hash == digest and existing.embedded:
        return ProfileResult(kind, changed=False, chars=len(text), text_hash=digest, truncated=truncated)

    # No session is held open across the network call.
    [vector] = await router.embed(EMBED_TASK, [text[:MAX_EMBED_CHARS]])
    if len(vector) != EMBEDDING_DIM:
        raise RuntimeError(
            f"embedding has {len(vector)} dims, radar_profile.embedding is vector({EMBEDDING_DIM}); "
            "the 'embed' task's model does not match the schema"
        )

    stmt = pg_insert(Profile).values(kind=kind, text=text, text_hash=digest, embedding=vector, updated_at=_now())
    stmt = stmt.on_conflict_do_update(
        index_elements=[Profile.kind],
        set_={
            "text": stmt.excluded.text,
            "text_hash": stmt.excluded.text_hash,
            "embedding": stmt.excluded.embedding,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    async with db.session() as s:
        await s.execute(stmt)
        await s.commit()
    return ProfileResult(kind, changed=True, chars=len(text), text_hash=digest, truncated=truncated)


def load_resume(path: str | Path) -> str:
    """Read a .md/.txt resume as UTF-8 (a BOM is tolerated). PDFs and other formats are rejected."""
    path = Path(path)
    if path.suffix.lower() not in RESUME_SUFFIXES:
        raise ValueError(
            f"unsupported resume format {path.suffix or '(no extension)'!r} for {path}: "
            "export your resume to a .md or .txt file first (PDF is not supported)"
        )
    if not path.is_file():
        raise FileNotFoundError(f"resume file not found: {path}")
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValueError(f"resume file is not valid UTF-8: {path} ({e.reason} at byte {e.start})") from e


def resolve_resume_path(path: str | Path | None, orbit_home: Path) -> Path:
    """An explicit path is used as given (relative to the cwd); the default is relative to ORBIT_HOME."""
    if path is not None:
        return Path(path)
    return orbit_home / DEFAULT_RESUME_PATH


async def set_resume(db: Database, router: ModelRouter, path: str | Path) -> ProfileResult:
    return await set_profile(db, router, "resume", load_resume(path))


async def set_goal(db: Database, router: ModelRouter, text: str) -> ProfileResult:
    return await set_profile(db, router, "goal", text)


async def get_profile(db: Database, kind: str) -> Profile | None:
    """The stored profile row for `kind` (detached, with its embedding), or None."""
    async with db.session() as s:
        return (await s.execute(select(Profile).where(Profile.kind == kind))).scalar_one_or_none()
