"""sync_company (M3.1) against real Postgres: upsert, change detection, close and reopen."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, update

from orbit.radar.models import NormalizedPosting
from orbit.radar.repo import SyncResult, sync_company
from orbit.storage.db import Database
from orbit.storage.models import EMBEDDING_DIM, Company, Notification, Posting

T0 = datetime(2026, 9, 1, 9, 0, 0)


def _np(external_id: str, *, source: str = "greenhouse", slug: str = "acme", **kw) -> NormalizedPosting:
    fields = {
        "title": f"Software Engineering Intern {external_id}",
        "url": f"https://boards.greenhouse.io/{slug}/jobs/{external_id}",
        "location_raw": "Toronto, ON",
        "department": "Engineering",
        "description_text": f"Build things ({external_id}).",
        "raw": {"id": external_id},
    }
    fields.update(kw)
    return NormalizedPosting(source=source, external_id=external_id, company_slug=slug, **fields)


async def _company(db: Database, slug: str = "acme", source: str = "greenhouse") -> Company:
    async with db.session() as s:
        company = Company(slug=slug, name=slug.title(), source=source, board_token=slug)
        s.add(company)
        await s.commit()
        return company


async def _sync(db: Database, company: Company, postings: list[NormalizedPosting], now: datetime) -> SyncResult:
    async with db.session() as s:
        result = await sync_company(s, company, postings, now=now)
        await s.commit()
    return result


async def _rows(db: Database, company: Company | None = None) -> dict[str, Posting]:
    async with db.session() as s:
        stmt = select(Posting)
        if company is not None:
            stmt = stmt.where(Posting.company_id == company.id)
        return {p.external_id: p for p in (await s.execute(stmt)).scalars()}


async def _mark_processed(db: Database, external_id: str, content_hash: str) -> None:
    """Simulate M4/M5 having extracted, embedded and scored a posting."""
    async with db.session() as s:
        await s.execute(
            update(Posting)
            .where(Posting.external_id == external_id)
            .values(
                extraction={"is_internship": True},
                extraction_status="ok",
                extraction_model="gpt-5-mini",
                extracted_at=T0,
                embedding=[1.0] + [0.0] * (EMBEDDING_DIM - 1),
                embedded_hash=content_hash,
                score=0.8,
                scored_at=T0,
            )
        )
        await s.commit()


async def test_first_sync_inserts_all(db: Database):
    company = await _company(db)
    postings = [_np("1"), _np("2"), _np("3", department=None, location_raw="")]

    result = await _sync(db, company, postings, T0)

    assert result == SyncResult(new=3)
    assert result.fetched == 3
    rows = await _rows(db)
    assert set(rows) == {"1", "2", "3"}
    p = rows["1"]
    assert p.company_id == company.id and p.source == "greenhouse"
    assert p.status == "open" and p.closed_at is None
    assert p.first_seen_at == T0 and p.last_seen_at == T0
    assert p.content_hash == postings[0].content_hash()
    assert p.extraction_status == "pending" and p.extraction is None and p.score is None
    assert p.raw == {"id": "1"}
    assert rows["3"].department is None and rows["3"].location_raw == ""


async def test_identical_second_sync_preserves_pipeline_state(db: Database):
    company = await _company(db)
    postings = [_np("1"), _np("2")]
    await _sync(db, company, postings, T0)
    await _mark_processed(db, "1", postings[0].content_hash())

    t1 = T0 + timedelta(minutes=30)
    result = await _sync(db, company, postings, t1)

    assert result == SyncResult(unchanged=2)
    p = (await _rows(db))["1"]
    assert p.first_seen_at == T0 and p.last_seen_at == t1
    assert p.extraction == {"is_internship": True} and p.extraction_status == "ok"
    assert p.extraction_model == "gpt-5-mini" and p.extracted_at == T0
    assert p.score == 0.8 and p.scored_at == T0
    assert p.embedded_hash == p.content_hash and p.embedding is not None


async def test_non_content_fields_refresh_without_reset(db: Database):
    """url and raw are not part of content_hash: refreshed, but the posting stays unchanged."""
    company = await _company(db)
    await _sync(db, company, [_np("1")], T0)
    await _mark_processed(db, "1", _np("1").content_hash())

    moved = _np("1", url="https://example.com/new-url", raw={"id": "1", "v": 2})
    result = await _sync(db, company, [moved], T0 + timedelta(hours=1))

    assert result == SyncResult(unchanged=1)
    p = (await _rows(db))["1"]
    assert p.url == "https://example.com/new-url" and p.raw == {"id": "1", "v": 2}
    assert p.extraction_status == "ok" and p.score == 0.8


async def test_changed_content_resets_extraction_and_score(db: Database):
    company = await _company(db)
    await _sync(db, company, [_np("1"), _np("2")], T0)
    old_hash = _np("1").content_hash()
    await _mark_processed(db, "1", old_hash)
    await _mark_processed(db, "2", _np("2").content_hash())

    changed = _np("1", description_text="Now with Rust.")
    result = await _sync(db, company, [changed, _np("2")], T0 + timedelta(hours=1))

    assert result == SyncResult(updated=1, unchanged=1)
    rows = await _rows(db)
    p = rows["1"]
    assert p.description_text == "Now with Rust."
    assert p.content_hash == changed.content_hash() != old_hash
    assert p.extraction is None and p.extraction_status == "pending"
    assert p.extraction_error is None and p.extraction_model is None and p.extracted_at is None
    assert p.score is None and p.scored_at is None
    # The old vector is kept but is stale, because embedded_hash no longer matches (M5.3 re-embeds).
    assert p.embedding is not None and p.embedded_hash == old_hash != p.content_hash
    assert p.first_seen_at == T0 and p.status == "open"
    # The untouched posting keeps everything.
    assert rows["2"].extraction_status == "ok" and rows["2"].score == 0.8


async def test_missing_posting_is_closed(db: Database):
    company = await _company(db)
    await _sync(db, company, [_np("1"), _np("2")], T0)

    t1 = T0 + timedelta(hours=1)
    result = await _sync(db, company, [_np("1")], t1)

    assert result == SyncResult(closed=1, unchanged=1)
    rows = await _rows(db)
    assert rows["2"].status == "closed" and rows["2"].closed_at == t1
    assert rows["2"].last_seen_at == T0
    assert rows["1"].status == "open" and rows["1"].closed_at is None

    # Already closed: a later sync without it does not close it again or move closed_at.
    result = await _sync(db, company, [_np("1")], t1 + timedelta(hours=1))
    assert result == SyncResult(unchanged=1)
    assert (await _rows(db))["2"].closed_at == t1


async def test_reappearing_posting_is_reopened(db: Database):
    company = await _company(db)
    await _sync(db, company, [_np("1"), _np("2"), _np("3")], T0)
    await _mark_processed(db, "2", _np("2").content_hash())
    await _mark_processed(db, "3", _np("3").content_hash())
    await _sync(db, company, [_np("1")], T0 + timedelta(hours=1))

    t2 = T0 + timedelta(hours=2)
    changed3 = _np("3", title="Data Engineering Intern")
    result = await _sync(db, company, [_np("1"), _np("2"), changed3], t2)

    assert result == SyncResult(reopened=2, unchanged=1)
    rows = await _rows(db)
    for ext in ("2", "3"):
        assert rows[ext].status == "open" and rows[ext].closed_at is None
        assert rows[ext].last_seen_at == t2 and rows[ext].first_seen_at == T0
    # Same content: extraction survives the close/reopen cycle.
    assert rows["2"].extraction_status == "ok" and rows["2"].score == 0.8
    # Changed content while closed: reset like any other change.
    assert rows["3"].extraction_status == "pending" and rows["3"].score is None
    assert rows["3"].title == "Data Engineering Intern"


async def test_empty_successful_fetch_closes_all_open_postings(db: Database):
    company = await _company(db)
    await _sync(db, company, [_np("1"), _np("2")], T0)

    t1 = T0 + timedelta(hours=1)
    result = await _sync(db, company, [], t1)

    assert result == SyncResult(closed=2)
    assert result.fetched == 0
    rows = await _rows(db)
    assert all(p.status == "closed" and p.closed_at == t1 for p in rows.values())


async def test_empty_fetch_for_company_without_postings_is_a_no_op(db: Database):
    company = await _company(db)
    assert await _sync(db, company, [], T0) == SyncResult()
    assert await _rows(db) == {}


async def test_duplicate_external_ids_in_one_fetch_last_wins(db: Database):
    company = await _company(db)
    postings = [_np("1", title="First copy"), _np("2"), _np("1", title="Second copy")]

    result = await _sync(db, company, postings, T0)

    assert result == SyncResult(new=2)
    rows = await _rows(db)
    assert len(rows) == 2 and rows["1"].title == "Second copy"

    # Duplicates of an existing row in a later fetch are fine too.
    result = await _sync(db, company, [_np("1", title="Second copy"), _np("1", title="Second copy"), _np("2")], T0)
    assert result == SyncResult(unchanged=2)


async def test_other_companies_and_sources_are_untouched(db: Database):
    acme = await _company(db, "acme", "greenhouse")
    lever_co = await _company(db, "beta", "lever")
    other_gh = await _company(db, "gamma", "greenhouse")
    await _sync(db, acme, [_np("1"), _np("2")], T0)
    await _sync(db, lever_co, [_np("1", source="lever", slug="beta", title="Lever role")], T0)
    await _sync(db, other_gh, [_np("9", slug="gamma")], T0)

    t1 = T0 + timedelta(hours=1)
    # Acme's "1" changes and "2" disappears; the lever posting with external_id "1" must not move.
    result = await _sync(db, acme, [_np("1", description_text="changed")], t1)
    assert result == SyncResult(updated=1, closed=1)

    lever_row = (await _rows(db, lever_co))["1"]
    assert lever_row.title == "Lever role" and lever_row.status == "open"
    assert lever_row.last_seen_at == T0 and lever_row.description_text == "Build things (1)."
    gamma_row = (await _rows(db, other_gh))["9"]
    assert gamma_row.status == "open" and gamma_row.last_seen_at == T0

    # An empty fetch for the lever company closes only its own posting.
    assert await _sync(db, lever_co, [], t1) == SyncResult(closed=1)
    assert (await _rows(db, acme))["1"].status == "open"
    assert (await _rows(db, other_gh))["9"].status == "open"


async def test_postings_from_another_source_are_rejected(db: Database):
    company = await _company(db)
    async with db.session() as s:
        with pytest.raises(ValueError, match="lever"):
            await sync_company(s, company, [_np("1", source="lever")], now=T0)
    assert await _rows(db) == {}


async def test_notification_rows_survive_update_close_and_reopen(db: Database):
    company = await _company(db)
    await _sync(db, company, [_np("1")], T0)
    posting_id = (await _rows(db))["1"].id
    async with db.session() as s:
        s.add(Notification(posting_id=posting_id, score=0.9, status="sent", sent_at=T0))
        await s.commit()

    await _sync(db, company, [_np("1", description_text="changed")], T0 + timedelta(hours=1))
    await _sync(db, company, [], T0 + timedelta(hours=2))
    await _sync(db, company, [_np("1", description_text="changed")], T0 + timedelta(hours=3))

    assert (await _rows(db))["1"].id == posting_id
    async with db.session() as s:
        notes = (await s.execute(select(Notification))).scalars().all()
    assert [(n.posting_id, n.status, n.sent_at) for n in notes] == [(posting_id, "sent", T0)]


async def test_sync_does_not_commit(db: Database):
    """The caller owns the transaction (M3.2 writes postings and the radar_runs row together)."""
    company = await _company(db)
    async with db.session() as s:
        result = await sync_company(s, company, [_np("1")], now=T0)
        assert result == SyncResult(new=1)
        await s.rollback()
    assert await _rows(db) == {}


async def test_aware_now_is_stored_as_naive_local(db: Database):
    company = await _company(db)
    aware = T0.astimezone()  # same instant, with the local offset attached
    await _sync(db, company, [_np("1")], aware)
    p = (await _rows(db))["1"]
    assert p.first_seen_at == T0 and p.first_seen_at.tzinfo is None


async def test_large_fetch_is_chunked(db: Database):
    company = await _company(db)
    postings = [_np(str(i)) for i in range(1200)]
    assert await _sync(db, company, postings, T0) == SyncResult(new=1200)
    result = await _sync(db, company, postings[:1100], T0 + timedelta(hours=1))
    assert result == SyncResult(unchanged=1100, closed=100)
