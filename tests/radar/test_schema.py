"""Radar tables (migration 0002_radar) against real Postgres + pgvector."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from orbit.storage.db import Database
from orbit.storage.models import EMBEDDING_DIM, Company, Notification, Posting, Profile, Run


def _unit(i: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[i] = 1.0
    return v


def _posting(company: Company, external_id: str, **kw) -> Posting:
    fields = {
        "source": company.source,
        "external_id": external_id,
        "title": "Software Engineering Intern",
        "url": f"https://boards.greenhouse.io/acme/jobs/{external_id}",
        "location_raw": "Toronto, ON",
        "description_text": "Build things.",
        "raw": {"id": external_id, "nested": {"ok": True}},
        "content_hash": "0" * 64,
    }
    fields.update(kw)
    return Posting(company_id=company.id, **fields)


async def _company(db: Database, slug: str = "acme") -> Company:
    async with db.session() as s:
        company = Company(slug=slug, name="Acme", source="greenhouse", board_token=slug)
        s.add(company)
        await s.commit()
        return company


async def test_company_and_posting_round_trip_with_defaults(db: Database):
    company = await _company(db)
    async with db.session() as s:
        s.add(_posting(company, "123", department="Engineering"))
        await s.commit()

    async with db.session() as s:
        p = (await s.execute(select(Posting))).scalar_one()
        c = await s.get(Company, company.id)
    assert c.active is True and c.created_at is not None and c.hq_city is None
    assert p.status == "open" and p.extraction_status == "pending"
    assert p.raw == {"id": "123", "nested": {"ok": True}}
    assert p.first_seen_at is not None and p.first_seen_at.tzinfo is None
    assert p.embedding is None and p.score is None and p.extraction is None


async def test_source_external_id_is_unique(db: Database):
    company = await _company(db)
    async with db.session() as s:
        s.add(_posting(company, "dup"))
        await s.commit()

    async with db.session() as s:
        s.add(_posting(company, "dup", title="Same posting, second insert"))
        with pytest.raises(IntegrityError, match="uq_radar_postings_source_external_id"):
            await s.commit()

    # Same external_id on a different source is a different posting.
    async with db.session() as s:
        lever = Company(slug="acme-lever", name="Acme", source="lever", board_token="acme")
        s.add(lever)
        await s.flush()
        s.add(_posting(lever, "dup", source="lever"))
        await s.commit()


async def test_embedding_round_trip_and_cosine_order(db: Database):
    company = await _company(db)
    near = _unit(0)
    near[1] = 0.1
    async with db.session() as s:
        s.add_all(
            [
                _posting(company, "far", embedding=_unit(5)),
                _posting(company, "near", embedding=near),
                _posting(company, "exact", embedding=_unit(0)),
                _posting(company, "none"),
            ]
        )
        await s.commit()

    query = _unit(0)
    async with db.session() as s:
        stored = (await s.execute(select(Posting.embedding).where(Posting.external_id == "near"))).scalar_one()
        dist = Posting.embedding.cosine_distance(query)
        rows = (
            await s.execute(
                select(Posting.external_id, dist).where(Posting.embedding.is_not(None)).order_by(dist)
            )
        ).all()

    assert len(stored) == EMBEDDING_DIM
    assert list(stored[:3]) == pytest.approx([1.0, 0.1, 0.0])
    assert [r[0] for r in rows] == ["exact", "near", "far"]
    assert rows[0][1] == pytest.approx(0.0) and rows[2][1] == pytest.approx(1.0)


async def test_wrong_embedding_dimension_is_rejected(db: Database):
    company = await _company(db)
    async with db.session() as s:
        s.add(_posting(company, "short", embedding=[1.0, 2.0, 3.0]))
        with pytest.raises(Exception, match="expected 1536 dimensions"):
            await s.commit()


async def test_one_notification_per_posting(db: Database):
    company = await _company(db)
    async with db.session() as s:
        posting = _posting(company, "n1")
        s.add(posting)
        await s.flush()
        s.add(Notification(posting_id=posting.id, score=0.9))
        await s.commit()
        n = (await s.execute(select(Notification))).scalar_one()
        assert n.status == "pending" and n.attempts == 0 and n.sent_at is None

    async with db.session() as s:
        s.add(Notification(posting_id=posting.id, score=0.95))
        with pytest.raises(IntegrityError, match="uq_radar_notifications_posting_id"):
            await s.commit()


@pytest.mark.parametrize(
    ("make", "constraint"),
    [
        (lambda c: _posting(c, "bad", status="archived"), "ck_radar_postings_status"),
        (lambda c: _posting(c, "bad", extraction_status="done"), "ck_radar_postings_extraction_status"),
        (lambda c: Company(slug="x", name="X", source="workday", board_token="x"), "ck_radar_companies_source"),
        (lambda c: Profile(kind="cover_letter", text="t", text_hash="h"), "ck_radar_profile_kind"),
    ],
)
async def test_check_constraints_reject_bad_enums(db: Database, make, constraint):
    company = await _company(db)
    async with db.session() as s:
        s.add(make(company))
        with pytest.raises(IntegrityError, match=constraint):
            await s.commit()


async def test_notification_status_check(db: Database):
    company = await _company(db)
    async with db.session() as s:
        posting = _posting(company, "p")
        s.add(posting)
        await s.flush()
        s.add(Notification(posting_id=posting.id, score=0.5, status="queued"))
        with pytest.raises(IntegrityError, match="ck_radar_notifications_status"):
            await s.commit()


async def test_profile_kind_unique_and_runs_cascade_with_company(db: Database):
    company = await _company(db)
    async with db.session() as s:
        s.add(Profile(kind="resume", text="cv", text_hash="a", embedding=_unit(1)))
        s.add(Run(company_id=company.id, ok=True, fetched=3, new=2))
        s.add(_posting(company, "c1"))
        await s.commit()
        run = (await s.execute(select(Run))).scalar_one()
        assert (run.fetched, run.new, run.closed) == (3, 2, 0) and run.started_at is not None

    async with db.session() as s:
        s.add(Profile(kind="resume", text="cv2", text_hash="b"))
        with pytest.raises(IntegrityError, match="uq_radar_profile_kind"):
            await s.commit()

    # Deleting a company removes its postings and runs (ON DELETE CASCADE).
    async with db.session() as s:
        await s.delete(await s.get(Company, company.id))
        await s.commit()
        assert (await s.execute(select(Posting))).first() is None
        assert (await s.execute(select(Run))).first() is None
