"""`orbit radar verify-sources`: fetch every active company's board live and record whether it works.

Verification only: postings are counted, never written. Each company's result goes to
`radar_companies.last_verified_at / last_verify_status ('ok'|'error') / last_verify_error`.
A board that answers 200 with no postings passes: the token is right, the company just isn't hiring.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, update

from orbit.radar.sources import PoliteClient, available_sources, get_adapter
from orbit.storage.db import Database
from orbit.storage.models import Company

# Intern-looking titles; "new grad" roles are counted separately since they are a different search.
INTERN_RE = re.compile(r"\b(intern|interns|internship|internships|co-?op|student)\b", re.IGNORECASE)
NEW_GRAD_RE = re.compile(r"\b(new[\s-]?grad(uate)?s?|early[\s-]career|university[\s-]grad(uate)?s?)\b", re.IGNORECASE)
MAX_ERROR_CHARS = 500


@dataclass
class VerifyResult:
    slug: str
    source: str
    board_token: str
    ok: bool
    postings: int = 0
    intern: int = 0
    new_grad: int = 0
    error: str | None = None


@dataclass
class SourceSummary:
    source: str
    companies: int = 0
    ok: int = 0
    postings: int = 0
    intern: int = 0
    new_grad: int = 0

    @property
    def errors(self) -> int:
        return self.companies - self.ok


@dataclass
class VerifyReport:
    results: list[VerifyResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(r.ok for r in self.results)

    def by_source(self) -> list[SourceSummary]:
        summaries = {s: SourceSummary(s) for s in sorted({r.source for r in self.results})}
        for r in self.results:
            s = summaries[r.source]
            s.companies += 1
            s.ok += r.ok
            s.postings += r.postings
            s.intern += r.intern
            s.new_grad += r.new_grad
        return list(summaries.values())


def count_titles(titles: Iterable[str]) -> tuple[int, int]:
    """(intern/co-op-looking titles, new-grad-looking titles)."""
    titles = list(titles)
    return sum(bool(INTERN_RE.search(t)) for t in titles), sum(bool(NEW_GRAD_RE.search(t)) for t in titles)


async def _check(client: PoliteClient, company: Company) -> VerifyResult:
    base = {"slug": company.slug, "source": company.source, "board_token": company.board_token}
    try:
        postings = await get_adapter(company.source).fetch(client, company)
    except Exception as e:  # one broken board (bad JSON, adapter bug) must not stop the others
        message = str(e) or e.__class__.__name__
        if not isinstance(e, RuntimeError):  # SourceFetchError is a RuntimeError with a self-explaining message
            message = f"{e.__class__.__name__}: {message}"
        return VerifyResult(**base, ok=False, error=message[:MAX_ERROR_CHARS])
    intern, new_grad = count_titles(p.title for p in postings)
    return VerifyResult(**base, ok=True, postings=len(postings), intern=intern, new_grad=new_grad)


async def verify_sources(db: Database, client: PoliteClient, *, source: str | None = None) -> VerifyReport:
    """Fetch every active company's board (optionally one source) and store the outcome on its row."""
    if source is not None and source not in available_sources():
        raise ValueError(f"unknown source {source!r}; known: {', '.join(available_sources())}")

    query = select(Company).where(Company.active.is_(True)).order_by(Company.source, Company.slug)
    if source is not None:
        query = query.where(Company.source == source)
    async with db.session() as session:  # read, then release the connection before the slow fetches
        companies = list((await session.scalars(query)).all())

    # PoliteClient enforces the 4-request cap and the 1 s per-host spacing, so gathering is safe.
    results = await asyncio.gather(*(_check(client, c) for c in companies))

    checked_at = datetime.now().replace(microsecond=0)
    async with db.session() as session:
        for r in results:
            await session.execute(
                update(Company)
                .where(Company.slug == r.slug)
                .values(
                    last_verified_at=checked_at,
                    last_verify_status="ok" if r.ok else "error",
                    last_verify_error=r.error,
                )
            )
        await session.commit()
    return VerifyReport(list(results))


def format_report(report: VerifyReport, min_ok: int) -> str:
    """Plain-text table per company, then a per-source summary and the pass/fail line."""
    header = f"{'slug':<18} {'source':<11} {'status':<6} {'postings':>8} {'intern':>6} {'newgrad':>7}  error"
    lines = [header, "-" * len(header)]
    for r in report.results:
        status = "ok" if r.ok else "ERROR"
        error = (r.error or "").replace("\n", " ")[:120]
        lines.append(f"{r.slug:<18} {r.source:<11} {status:<6} {r.postings:>8} {r.intern:>6} {r.new_grad:>7}  {error}")
    lines.append("")
    sum_header = f"{'source':<11} {'companies':>9} {'ok':>4} {'error':>5} {'postings':>8} {'intern':>6} {'newgrad':>7}"
    lines += [sum_header, "-" * len(sum_header)]
    for s in report.by_source():
        lines.append(
            f"{s.source:<11} {s.companies:>9} {s.ok:>4} {s.errors:>5} {s.postings:>8} {s.intern:>6} {s.new_grad:>7}"
        )
    total = len(report.results)
    verdict = "PASS" if report.ok_count >= min_ok else "FAIL"
    lines.append("")
    lines.append(f"{verdict}: {report.ok_count}/{total} companies OK (need at least {min_ok})")
    return "\n".join(lines)
