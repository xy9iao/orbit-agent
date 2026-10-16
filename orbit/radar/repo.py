"""Posting persistence for the radar: sync one company's fetched postings into `radar_postings`.

`sync_company` takes an `AsyncSession` and never commits. The caller owns the transaction, so
M3.2 can write a company's postings and its `radar_runs` row atomically (both or neither).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import String, all_, any_, bindparam, case, literal_column, null, select, update
from sqlalchemy.dialects.postgresql import ARRAY, insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.radar.models import NormalizedPosting, to_naive_local
from orbit.storage.models import Posting

# asyncpg allows 32767 bind parameters per statement; 13 columns x 500 rows stays far below it.
_UPSERT_CHUNK = 500

# Pipeline state that belongs to the old content: cleared when content_hash changes.
_RESET_ON_CHANGE = ("extraction", "extraction_error", "extraction_model", "extracted_at", "score", "scored_at")


class CompanyLike(Protocol):
    id: int
    source: str


@dataclass(frozen=True)
class SyncResult:
    """Counts from one successful fetch. new/updated/reopened/unchanged partition the fetched set.

    - new: never seen before (inserted).
    - updated: was open, content_hash changed (extraction and score reset to pending).
    - reopened: was closed and reappeared (extraction also reset if its content changed).
    - unchanged: was open, same content_hash (only last_seen_at and display fields refreshed).
    - closed: was open for this company, missing from the fetch.
    """

    new: int = 0
    updated: int = 0
    closed: int = 0
    reopened: int = 0
    unchanged: int = 0

    @property
    def fetched(self) -> int:
        return self.new + self.updated + self.reopened + self.unchanged


def _dedupe(postings: Sequence[NormalizedPosting]) -> dict[str, NormalizedPosting]:
    """Last occurrence wins, so one INSERT never touches the same (source, external_id) twice."""
    return {p.external_id: p for p in postings}


async def sync_company(
    session: AsyncSession,
    company: CompanyLike,
    postings: Sequence[NormalizedPosting],
    *,
    now: datetime | None = None,
) -> SyncResult:
    """Upsert a company's postings from a *successful* fetch and close the ones that disappeared.

    Only call this when the fetch succeeded: a failed fetch must close nothing (M3.2 records the
    failure instead). A successful but empty fetch is taken at face value, meaning the board is
    empty, so every open posting of this company is closed.

    Postings are keyed on (company.source, external_id); every posting must come from
    `company.source` (ValueError otherwise). `radar_notifications` is never touched, so a posting
    that is updated, closed or reopened is still never notified twice.

    Does not commit: the caller commits (or rolls back) the session.
    """
    now = to_naive_local(now) if now is not None else datetime.now().replace(microsecond=0)
    fetched = _dedupe(postings)
    wrong = sorted({p.source for p in fetched.values()} - {company.source})
    if wrong:
        raise ValueError(f"postings from source(s) {wrong} passed to a {company.source!r} company")

    t = Posting.__table__
    ids = list(fetched)

    # One array parameter instead of an IN list, so big boards never hit the bind-parameter limit.
    ids_param = bindparam("fetched_ids", ids, type_=ARRAY(String))

    # Old state, locked so the classification below matches what the upsert sees.
    rows = await session.execute(
        select(t.c.external_id, t.c.content_hash, t.c.status)
        .where(t.c.source == company.source, t.c.external_id == any_(ids_param))
        .with_for_update()
    )
    previous = {r.external_id: (r.content_hash, r.status) for r in rows}

    new = updated = reopened = unchanged = 0
    items = list(fetched.values())
    for i in range(0, len(items), _UPSERT_CHUNK):
        rows_in = [
            {
                "source": company.source,
                "external_id": p.external_id,
                "company_id": company.id,
                "title": p.title,
                "url": p.url,
                "location_raw": p.location_raw,
                "department": p.department,
                "description_text": p.description_text,
                "raw": p.raw,
                "content_hash": p.content_hash(),
                "status": "open",
                "first_seen_at": now,
                "last_seen_at": now,
            }
            for p in items[i : i + _UPSERT_CHUNK]
        ]
        stmt = pg_insert(t).values(rows_in)
        ex = stmt.excluded
        same = t.c.content_hash == ex.content_hash
        # In ON CONFLICT DO UPDATE, `radar_postings.col` is the old row, so every CASE compares
        # against the old hash no matter where content_hash appears in the SET list.
        set_ = {
            "company_id": ex.company_id,
            "title": ex.title,
            "url": ex.url,
            "location_raw": ex.location_raw,
            "department": ex.department,
            "description_text": ex.description_text,
            "raw": ex.raw,
            "content_hash": ex.content_hash,
            "last_seen_at": ex.last_seen_at,
            "status": "open",
            "closed_at": null(),
            "extraction_status": case((same, t.c.extraction_status), else_="pending"),
            **{col: case((same, t.c[col]), else_=null()) for col in _RESET_ON_CHANGE},
            # embedding/embedded_hash stay: embedded_hash != content_hash marks it stale (M5.3).
        }
        stmt = stmt.on_conflict_do_update(
            constraint="uq_radar_postings_source_external_id", set_=set_
        ).returning(t.c.external_id, literal_column("(xmax = 0)").label("inserted"))
        hashes = {r["external_id"]: r["content_hash"] for r in rows_in}

        for r in await session.execute(stmt):
            old = previous.get(r.external_id)
            if r.inserted or old is None:
                # old is None but not inserted only if another writer raced us; call it new.
                new += 1
            elif old[1] == "closed":
                reopened += 1
            elif old[0] != hashes[r.external_id]:
                updated += 1
            else:
                unchanged += 1

    close = (
        update(t)
        .where(t.c.company_id == company.id, t.c.source == company.source, t.c.status == "open")
        .values(status="closed", closed_at=now)
        .returning(t.c.id)
    )
    if ids:  # explicit: an empty successful fetch closes every open posting of this company
        close = close.where(t.c.external_id != all_(ids_param))
    closed = len((await session.execute(close)).all())

    return SyncResult(new=new, updated=updated, closed=closed, reopened=reopened, unchanged=unchanged)
