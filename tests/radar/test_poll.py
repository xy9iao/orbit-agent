"""poll_all / poll_company (M3.2): recorded fixtures through httpx.MockTransport, real Postgres."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

import orbit.radar.poll as poll_module
from orbit.radar.models import NormalizedPosting
from orbit.radar.poll import CompanyPollResult, describe_error, poll_all, poll_company
from orbit.radar.sources.http import PoliteClient, SourceFetchError
from orbit.storage.db import Database
from orbit.storage.models import Company, Posting, Run

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "radar"
GREENHOUSE_FAIRE = FIXTURES / "greenhouse_faire.json"
GH_HOST = "boards-api.greenhouse.io"

T0 = datetime(2026, 9, 1, 9, 0, 0)
T1 = T0 + timedelta(minutes=30)
T2 = T0 + timedelta(minutes=60)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any request that escapes a MockTransport fails loudly instead of reaching the internet."""

    async def _refuse(self, request):
        raise AssertionError(f"test tried to reach the network: {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _refuse)


def faire_payload() -> dict[str, Any]:
    return json.loads(GREENHOUSE_FAIRE.read_text(encoding="utf-8"))


def acme_payload() -> dict[str, Any]:
    """A hand-built second Greenhouse board."""
    return {
        "jobs": [
            {
                "id": 9001,
                "title": "Software Developer Co-op (Winter 2027)",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/9001",
                "location": {"name": "Waterloo, ON"},
                "departments": [{"name": "Engineering"}],
                "content": "&lt;p&gt;Build developer tools.&lt;/p&gt;",
                "updated_at": "2026-08-30T10:00:00-04:00",
                "first_published": "2026-08-20T10:00:00-04:00",
            }
        ],
        "meta": {"total": 1},
    }


class FakeBoards:
    """Serves Greenhouse boards by token: a payload dict, or an int HTTP status to fail with."""

    def __init__(self, **boards: dict[str, Any] | int):
        self.boards: dict[str, dict[str, Any] | int] = dict(boards)
        self.requests: list[httpx.Request] = []
        self.on_request = None  # optional async hook(request), called before responding

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == GH_HOST
        if self.on_request is not None:
            await self.on_request(request)
        token = request.url.path.split("/")[3]  # /v1/boards/{token}/jobs
        board = self.boards.get(token, 404)
        if isinstance(board, int):
            return httpx.Response(board, json={"status": board, "error": "nope"})
        return httpx.Response(200, json=board)

    def tokens_requested(self) -> list[str]:
        return [r.url.path.split("/")[3] for r in self.requests]


async def _no_sleep(_seconds: float) -> None:
    return None


def make_client(boards: FakeBoards) -> PoliteClient:
    return PoliteClient(transport=httpx.MockTransport(boards), sleep=_no_sleep)


async def _company(db: Database, slug: str, *, active: bool = True) -> Company:
    async with db.session() as s:
        company = Company(slug=slug, name=slug.title(), source="greenhouse", board_token=slug, active=active)
        s.add(company)
        await s.commit()
        return company


async def _postings(db: Database, company: Company) -> dict[str, Posting]:
    async with db.session() as s:
        rows = await s.execute(select(Posting).where(Posting.company_id == company.id))
        return {p.external_id: p for p in rows.scalars()}


async def _runs(db: Database, company: Company) -> list[Run]:
    async with db.session() as s:
        rows = await s.execute(select(Run).where(Run.company_id == company.id).order_by(Run.id))
        return list(rows.scalars())


async def _poll(db: Database, boards: FakeBoards, now: datetime, **kw):
    async with make_client(boards) as client:
        return await poll_all(db, client, now=now, **kw)


def _by_slug(summary) -> dict[str, CompanyPollResult]:
    return {r.slug: r for r in summary.results}


async def test_first_poll_inserts_every_posting(db: Database):
    faire = await _company(db, "faire")
    n = len(faire_payload()["jobs"])
    boards = FakeBoards(faire=faire_payload())

    summary = await _poll(db, boards, T0)

    assert boards.tokens_requested() == ["faire"]
    [result] = summary.results
    assert result.ok and result.error is None
    assert (result.fetched, result.new, result.updated, result.closed, result.reopened) == (n, n, 0, 0, 0)
    assert (summary.companies, summary.ok, summary.failed, summary.fetched, summary.new) == (1, 1, 0, n, n)

    rows = await _postings(db, faire)
    assert set(rows) == {str(j["id"]) for j in faire_payload()["jobs"]}
    assert all(p.status == "open" and p.first_seen_at == T0 for p in rows.values())

    [run] = await _runs(db, faire)
    assert run.id == result.run_id
    assert run.ok is True and run.error is None
    assert (run.fetched, run.new, run.closed) == (n, n, 0)
    assert run.started_at == T0 and run.finished_at == T0


async def test_identical_second_poll_inserts_nothing(db: Database):
    faire = await _company(db, "faire")
    n = len(faire_payload()["jobs"])
    boards = FakeBoards(faire=faire_payload())
    await _poll(db, boards, T0)
    before = {k: (p.id, p.content_hash, p.status, p.first_seen_at) for k, p in (await _postings(db, faire)).items()}

    summary = await _poll(db, boards, T1)

    [result] = summary.results
    assert result.ok
    assert (result.fetched, result.new, result.updated, result.closed, result.unchanged) == (n, 0, 0, 0, n)
    after = await _postings(db, faire)
    assert {k: (p.id, p.content_hash, p.status, p.first_seen_at) for k, p in after.items()} == before
    assert all(p.last_seen_at == T1 for p in after.values())

    runs = await _runs(db, faire)
    assert len(runs) == 2
    assert runs[1].ok is True and (runs[1].fetched, runs[1].new, runs[1].closed) == (n, 0, 0)


async def test_removed_posting_is_closed(db: Database):
    faire = await _company(db, "faire")
    payload = faire_payload()
    boards = FakeBoards(faire=payload)
    await _poll(db, boards, T0)

    trimmed = copy.deepcopy(payload)
    gone = trimmed["jobs"].pop(1)
    boards.boards["faire"] = trimmed
    summary = await _poll(db, boards, T1)

    [result] = summary.results
    assert result.ok and result.closed == 1 and result.new == 0
    rows = await _postings(db, faire)
    closed = rows[str(gone["id"])]
    assert closed.status == "closed" and closed.closed_at == T1
    assert all(p.status == "open" for k, p in rows.items() if k != str(gone["id"]))
    assert (await _runs(db, faire))[-1].closed == 1


@pytest.mark.parametrize("status", [500, 404])
async def test_fetch_error_closes_nothing_and_other_company_still_succeeds(db: Database, status: int):
    acme = await _company(db, "acme")
    faire = await _company(db, "faire")
    boards = FakeBoards(faire=faire_payload(), acme=acme_payload())
    await _poll(db, boards, T0)
    open_before = {k: (p.status, p.last_seen_at) for k, p in (await _postings(db, faire)).items()}

    boards.boards["faire"] = status
    boards.requests.clear()
    summary = await _poll(db, boards, T1)

    # 500 is retried (max_retries=3 -> 4 attempts), 404 is not.
    expected_attempts = 4 if status == 500 else 1
    assert boards.tokens_requested().count("faire") == expected_attempts
    results = _by_slug(summary)
    assert [r.slug for r in summary.results] == ["acme", "faire"]  # ordered by slug
    assert (summary.ok, summary.failed) == (1, 1)

    failed = results["faire"]
    assert not failed.ok and failed.fetched == 0 and failed.closed == 0
    assert failed.error.startswith("SourceFetchError: ") and f"HTTP {status}" in failed.error
    # The open set is untouched: nothing closed, last_seen_at not bumped.
    assert {k: (p.status, p.last_seen_at) for k, p in (await _postings(db, faire)).items()} == open_before

    run = (await _runs(db, faire))[-1]
    assert run.id == failed.run_id
    assert run.ok is False and run.error == failed.error
    assert run.started_at == T1 and run.finished_at == T1
    assert (run.fetched, run.new, run.closed) == (0, 0, 0)

    ok = results["acme"]
    assert ok.ok and ok.unchanged == 1 and ok.error is None
    acme_run = (await _runs(db, acme))[-1]
    assert acme_run.ok is True and acme_run.fetched == 1
    assert (await _postings(db, acme))["9001"].last_seen_at == T1


async def test_reappearing_posting_reopens(db: Database):
    faire = await _company(db, "faire")
    payload = faire_payload()
    gone_id = str(payload["jobs"][0]["id"])
    boards = FakeBoards(faire=payload)
    await _poll(db, boards, T0)

    trimmed = copy.deepcopy(payload)
    trimmed["jobs"].pop(0)
    boards.boards["faire"] = trimmed
    await _poll(db, boards, T1)
    assert (await _postings(db, faire))[gone_id].status == "closed"

    boards.boards["faire"] = payload
    summary = await _poll(db, boards, T2)

    [result] = summary.results
    assert result.ok and result.reopened == 1 and result.new == 0 and result.closed == 0
    p = (await _postings(db, faire))[gone_id]
    assert p.status == "open" and p.closed_at is None
    assert p.first_seen_at == T0 and p.last_seen_at == T2


async def test_inactive_companies_are_skipped(db: Database):
    faire = await _company(db, "faire")
    acme = await _company(db, "acme", active=False)
    boards = FakeBoards(faire=faire_payload(), acme=acme_payload())

    summary = await _poll(db, boards, T0)

    assert [r.slug for r in summary.results] == ["faire"]
    assert boards.tokens_requested() == ["faire"]
    assert await _runs(db, acme) == [] and await _postings(db, acme) == {}
    assert len(await _runs(db, faire)) == 1


async def test_explicit_company_list_overrides_active_filter(db: Database):
    await _company(db, "faire")
    acme = await _company(db, "acme", active=False)
    boards = FakeBoards(faire=faire_payload(), acme=acme_payload())

    summary = await _poll(db, boards, T0, companies=[acme])

    assert [r.slug for r in summary.results] == ["acme"]
    assert boards.tokens_requested() == ["acme"]


async def test_no_session_held_during_fetch_and_run_row_is_open(db: Database):
    """The run row is committed before the fetch (ok NULL), and no DB connection is checked out meanwhile."""
    faire = await _company(db, "faire")
    boards = FakeBoards(faire=faire_payload())
    seen: dict[str, Any] = {}

    async def during_fetch(_request: httpx.Request) -> None:
        seen["checked_out"] = db.engine.pool.checkedout()
        seen["runs"] = [(r.ok, r.finished_at) for r in await _runs(db, faire)]

    boards.on_request = during_fetch
    async with make_client(boards) as client:
        result = await poll_company(db, client, faire, now=T0)

    assert result.ok
    assert seen["checked_out"] == 0
    assert seen["runs"] == [(None, None)]


async def test_sync_failure_rolls_back_and_records_run(db: Database, monkeypatch):
    """An exception after the fetch (here: sync_company's source check) writes no postings and a failed run."""
    faire = await _company(db, "faire")
    wrong_source = [
        NormalizedPosting(source="lever", external_id="x1", company_slug="faire", title="Intern", url="https://x/1")
    ]

    class StubAdapter:
        async def fetch(self, client, company):
            return wrong_source

    monkeypatch.setattr(poll_module, "get_adapter", lambda source: StubAdapter())
    async with make_client(FakeBoards()) as client:
        result = await poll_company(db, client, faire, now=T0)

    assert not result.ok and result.error.startswith("ValueError: ")
    assert result.fetched == 1
    assert await _postings(db, faire) == {}
    [run] = await _runs(db, faire)
    assert run.id == result.run_id
    assert run.ok is False and run.error == result.error and run.fetched == 1 and run.finished_at == T0


def test_describe_error_is_short_and_typed():
    err = SourceFetchError("first line\nsecond line", url="https://x")
    assert describe_error(err) == "SourceFetchError: first line"
    assert describe_error(ValueError()) == "ValueError"
    assert len(describe_error(RuntimeError("x" * 2000))) == 500
