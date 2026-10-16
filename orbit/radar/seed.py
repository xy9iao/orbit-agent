"""Seed companies: validate `seeds.yaml` and upsert it into `radar_companies` by slug.

Ownership rules:
- the file owns slug, name, source, board_token and hq_city: seeding overwrites them;
- the DB owns `active`: new rows start active, existing rows keep their flag unless the entry sets
  `active:` explicitly (so a company switched off in the DB stays off across re-seeds);
- a company removed from the file is never deleted (that would cascade away its postings and the
  notification records that stop duplicate alerts); it is reported, and only `prune=True` deactivates it;
- when source or board_token changes, the old `last_verify_*` result no longer applies and is cleared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.radar.sources import available_sources
from orbit.storage.models import Company

DEFAULT_SEEDS_PATH = Path(__file__).with_name("seeds.yaml")
SEED_FIELDS = ("name", "source", "board_token", "hq_city")


class SeedCompany(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=100)
    name: str = Field(min_length=1, max_length=200)
    source: str
    board_token: str = Field(min_length=1, max_length=200)
    hq_city: str | None = Field(default=None, max_length=100)
    active: bool | None = None  # None: leave the DB flag alone (new rows start active)

    @field_validator("source")
    @classmethod
    def _known_source(cls, v: str) -> str:
        if v not in available_sources():
            raise ValueError(f"unknown source {v!r}; known: {', '.join(available_sources())}")
        return v


class SeedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    companies: list[SeedCompany] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> SeedFile:
        slugs: set[str] = set()
        boards: dict[tuple[str, str], str] = {}
        for c in self.companies:
            if c.slug in slugs:
                raise ValueError(f"duplicate slug {c.slug!r}")
            slugs.add(c.slug)
            # Two slugs on one board would poll it twice and fight over the same (source, external_id) rows.
            board = (c.source, c.board_token.lower())
            if board in boards:
                raise ValueError(f"{c.slug!r} and {boards[board]!r} both use {c.source} board {c.board_token!r}")
            boards[board] = c.slug
        return self


def parse_seeds(data: Any, origin: str = "seeds") -> list[SeedCompany]:
    """Validate already-loaded YAML data; raises ValueError with every problem listed."""
    try:
        return SeedFile.model_validate(data).companies
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '(file)'}: {err['msg']}" for err in e.errors()
        )
        raise ValueError(f"invalid seed file {origin}: {problems}") from None


def load_seeds(path: Path | str = DEFAULT_SEEDS_PATH) -> list[SeedCompany]:
    """Read and validate a seeds YAML file (FileNotFoundError if missing, ValueError if invalid)."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML in {path}: {e}") from None
    return parse_seeds(data, str(path))


@dataclass
class SeedResult:
    inserted: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # in the DB, not in the file
    deactivated: list[str] = field(default_factory=list)  # missing ones switched off by prune=True


async def seed_companies(session: AsyncSession, seeds: list[SeedCompany], *, prune: bool = False) -> SeedResult:
    """Upsert `seeds` by slug. Flushes but never commits: the caller owns the transaction."""
    existing = {c.slug: c for c in (await session.scalars(select(Company).with_for_update())).all()}
    result = SeedResult()

    for seed in seeds:
        row = existing.get(seed.slug)
        if row is None:
            values = {f: getattr(seed, f) for f in SEED_FIELDS}
            session.add(Company(slug=seed.slug, active=seed.active if seed.active is not None else True, **values))
            result.inserted.append(seed.slug)
            continue

        changed = False
        board_moved = row.source != seed.source or row.board_token != seed.board_token
        for f in SEED_FIELDS:
            if getattr(row, f) != getattr(seed, f):
                setattr(row, f, getattr(seed, f))
                changed = True
        if seed.active is not None and row.active != seed.active:
            row.active = seed.active
            changed = True
        if board_moved:
            row.last_verified_at = row.last_verify_status = row.last_verify_error = None
        (result.updated if changed else result.unchanged).append(seed.slug)

    wanted = {s.slug for s in seeds}
    for slug in sorted(set(existing) - wanted):
        result.missing.append(slug)
        if prune and existing[slug].active:
            existing[slug].active = False
            result.deactivated.append(slug)

    await session.flush()
    return result
