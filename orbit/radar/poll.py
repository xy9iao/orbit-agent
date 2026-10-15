"""Poll every active company's job board once and sync the results into Postgres (M3.2).

Per company:
1. insert a `radar_runs` row (started_at, ok NULL) and commit it, so a crash or hang mid-poll
   leaves a visible unfinished run;
2. fetch the board through its adapter with *no* DB session open (the network can be slow);
3. on success, in ONE transaction: `sync_company` + mark the run ok with its counts, then commit;
4. on any failure: close nothing, mark the run ok=False with a short error, log a warning.

Companies are polled concurrently; the shared `PoliteClient` enforces the global request cap and
per-host spacing. One company's failure never affects another's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from loguru import logger
from sqlalchemy import insert, select, update

from orbit.radar.models import to_naive_local
from orbit.radar.repo import sync_company
from orbit.radar.sources import get_adapter
from orbit.radar.sources.http import PoliteClient
from orbit.storage.db import Database
from orbit.storage.models import Company, Run

_ERROR_MAX = 500


class PollableCompany(Protocol):
    """What polling needs from a company; the ORM `Company` satisfies it."""

    id: int
    slug: str
    source: str
    board_token: str


@dataclass(frozen=True)
class CompanyPollResult:
    slug: str
    ok: bool
    fetched: int = 0
    new: int = 0
    updated: int = 0
    closed: int = 0
    reopened: int = 0
    unchanged: int = 0
    error: str | None = None
    run_id: int | None = None


@dataclass(frozen=True)
class PollSummary:
    results: list[CompanyPollResult] = field(default_factory=list)

    def _sum(self, attr: str) -> int:
        return sum(getattr(r, attr) for r in self.results)

    @property
    def companies(self) -> int:
        return len(self.results)

    @property
    def ok(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    @property
    def fetched(self) -> int:
        return self._sum("fetched")

    @property
    def new(self) -> int:
        return self._sum("new")

    @property
    def updated(self) -> int:
        return self._sum("updated")

    @property
    def closed(self) -> int:
        return self._sum("closed")

    @property
    def reopened(self) -> int:
        return self._sum("reopened")

    @property
    def unchanged(self) -> int:
        return self._sum("unchanged")


def describe_error(exc: BaseException) -> str:
    """Exception type plus the first line of its message, at most 500 characters."""
    lines = str(exc).strip().splitlines()
    text = f"{type(exc).__name__}: {lines[0]}" if lines else type(exc).__name__
    return text[:_ERROR_MAX]


def _stamp(now: datetime | None) -> datetime:
    return to_naive_local(now) if now is not None else datetime.now().replace(microsecond=0)


async def _start_run(db: Database, company_id: int, started_at: datetime) -> int:
    async with db.session() as s:
        run_id = await s.scalar(
            insert(Run).values(company_id=company_id, started_at=started_at).returning(Run.id)
        )
        await s.commit()
    return int(run_id)


async def _fail_run(db: Database, run_id: int | None, *, error: str, fetched: int, now: datetime | None) -> None:
    """Best effort: if the DB itself is the problem, the run stays ok=NULL and only the log has the error."""
    if run_id is None:
        return
    try:
        async with db.session() as s:
            await s.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(finished_at=_stamp(now), ok=False, fetched=fetched, error=error)
            )
            await s.commit()
    except Exception as e:  # recording a failure must not raise
        logger.error("radar poll: could not record failed run {}: {}", run_id, describe_error(e))


async def poll_company(
    db: Database, client: PoliteClient, company: PollableCompany, *, now: datetime | None = None
) -> CompanyPollResult:
    """Fetch and sync one company. Never raises (except on cancellation); failures are in the result."""
    slug = company.slug
    run_id: int | None = None
    fetched = 0
    try:
        run_id = await _start_run(db, company.id, _stamp(now))
        # No session is open while we wait on the network.
        postings = await get_adapter(company.source).fetch(client, company)
        fetched = len(postings)
        async with db.session() as s:
            result = await sync_company(s, company, postings, now=now)
            await s.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(
                    finished_at=_stamp(now),
                    ok=True,
                    fetched=result.fetched,
                    new=result.new,
                    closed=result.closed,
                )
            )
            await s.commit()
    except Exception as e:  # one company's failure must not stop the poll
        error = describe_error(e)
        logger.warning("radar poll: {} ({}) failed: {}", slug, company.source, error)
        await _fail_run(db, run_id, error=error, fetched=fetched, now=now)
        return CompanyPollResult(slug=slug, ok=False, fetched=fetched, error=error, run_id=run_id)

    return CompanyPollResult(
        slug=slug,
        ok=True,
        fetched=result.fetched,
        new=result.new,
        updated=result.updated,
        closed=result.closed,
        reopened=result.reopened,
        unchanged=result.unchanged,
        run_id=run_id,
    )


async def active_companies(db: Database) -> list[Company]:
    async with db.session() as s:
        rows = await s.execute(select(Company).where(Company.active.is_(True)).order_by(Company.slug))
        return list(rows.scalars())


async def poll_all(
    db: Database,
    client: PoliteClient,
    *,
    companies: Sequence[PollableCompany] | None = None,
    now: datetime | None = None,
) -> PollSummary:
    """Poll `companies` (default: every active company, by slug) concurrently; results keep that order."""
    if companies is None:
        companies = await active_companies(db)
    outcomes = await asyncio.gather(
        *(poll_company(db, client, c, now=now) for c in companies), return_exceptions=True
    )
    results: list[CompanyPollResult] = []
    for company, outcome in zip(companies, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            # poll_company catches Exception; this only sees cancellation-like BaseExceptions.
            if not isinstance(outcome, Exception):
                raise outcome
            outcome = CompanyPollResult(slug=company.slug, ok=False, error=describe_error(outcome))
        results.append(outcome)
    summary = PollSummary(results)
    logger.info(
        "radar poll: {}/{} companies ok, fetched {}, new {}, updated {}, closed {}, reopened {}",
        summary.ok,
        summary.companies,
        summary.fetched,
        summary.new,
        summary.updated,
        summary.closed,
        summary.reopened,
    )
    return summary
