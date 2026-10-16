"""`orbit radar verify-sources` (M2.6), with every board answered by httpx.MockTransport (never the network)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

import orbit.radar.cli as radar_cli
from orbit.cli.main import app
from orbit.radar.sources import PoliteClient
from orbit.radar.verify import VerifyReport, VerifyResult, count_titles, format_report, verify_sources
from orbit.storage.db import Database
from orbit.storage.models import Company, Posting
from tests.radar.test_seed import patch_runtime, sql


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any request that escapes a MockTransport fails loudly instead of reaching the internet."""

    async def _refuse(self, request):
        raise AssertionError(f"test tried to reach the network: {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _refuse)


def _gh_job(i: int, title: str) -> dict:
    return {
        "id": i,
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{i}",
        "location": {"name": "Toronto, ON"},
        "content": "&lt;p&gt;Build things.&lt;/p&gt;",
        "first_published": "2026-09-01T10:00:00-04:00",
        "updated_at": "2026-09-02T10:00:00-04:00",
        "departments": [{"name": "Engineering"}],
    }


GREENHOUSE_ACME = {
    "jobs": [
        _gh_job(1, "Software Engineering Intern (Winter 2027)"),
        _gh_job(2, "Data Co-op"),
        _gh_job(3, "New Grad Software Engineer"),
        _gh_job(4, "Senior Backend Engineer"),
        _gh_job(5, "Internal Tools Engineer"),  # "Internal" is not "intern"
    ]
}
ASHBY_DELTA = {
    "jobs": [
        {"id": "a1", "title": "ML Research Intern", "isListed": True},
        {"id": "a2", "title": "Hidden Intern", "isListed": False},  # unlisted: the adapter drops it
    ]
}

# (source, slug, board_token, active)
COMPANIES = [
    ("greenhouse", "acme", "acme", True),
    ("lever", "beta", "beta", True),  # 404
    ("ashby", "gamma", "gamma", True),  # 503 forever
    ("ashby", "delta", "delta", True),
    ("ashby", "epsilon", "epsilon", True),  # 200, but a job without "id": adapter raises KeyError
    ("ashby", "zeta", "zeta", True),  # 200 with no postings: the board exists, so it passes
    ("greenhouse", "dormant", "dormant", False),  # inactive: never fetched
]


def handler(seen: list[str]):
    def _handle(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.host}{request.url.path}")
        path = request.url.path
        if path == "/v1/boards/acme/jobs":
            return httpx.Response(200, json=GREENHOUSE_ACME)
        if path == "/v0/postings/beta":
            return httpx.Response(404, json={"ok": False, "error": "Document not found"})
        if path.endswith("/gamma"):
            return httpx.Response(503, text="unavailable")
        if path.endswith("/delta"):
            return httpx.Response(200, json=ASHBY_DELTA)
        if path.endswith("/epsilon"):
            return httpx.Response(200, json={"jobs": [{"title": "No id"}]})
        if path.endswith("/zeta"):
            return httpx.Response(200, json={"jobs": []})
        raise AssertionError(f"unexpected request {request.url}")

    return _handle


def make_client(seen: list[str], sleeps: list[float]) -> PoliteClient:
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return PoliteClient(transport=httpx.MockTransport(handler(seen)), sleep=fake_sleep, max_retries=2)


async def _add_companies(db: Database) -> None:
    async with db.session() as s:
        for source, slug, token, active in COMPANIES:
            s.add(Company(slug=slug, name=slug.title(), source=source, board_token=token, active=active))
        await s.commit()


async def test_verify_sources_records_ok_and_errors(db: Database):
    await _add_companies(db)
    seen: list[str] = []
    sleeps: list[float] = []
    before = datetime.now().replace(microsecond=0)

    async with make_client(seen, sleeps) as client:
        report = await verify_sources(db, client)

    results = {r.slug: r for r in report.results}
    assert [r.slug for r in report.results] == ["delta", "epsilon", "gamma", "zeta", "acme", "beta"]
    assert "dormant" not in results and not any("dormant" in u for u in seen)

    acme = results["acme"]
    assert (acme.ok, acme.postings, acme.intern, acme.new_grad, acme.error) == (True, 5, 2, 1, None)
    assert (results["delta"].ok, results["delta"].postings, results["delta"].intern) == (True, 1, 1)
    assert (results["zeta"].ok, results["zeta"].postings) == (True, 0)

    assert not results["beta"].ok and "HTTP 404" in results["beta"].error
    assert seen.count("api.lever.co/v0/postings/beta") == 1  # 404 is final: no retries
    assert not results["gamma"].ok and "failed after 3 attempts" in results["gamma"].error
    assert seen.count("api.ashbyhq.com/posting-api/job-board/gamma") == 3  # 1 try + 2 retries
    assert 1.0 in sleeps and 2.0 in sleeps  # exponential backoff, slept through the injected fake
    assert not results["epsilon"].ok and results["epsilon"].error == "KeyError: 'id'"
    assert report.ok_count == 3

    async with db.session() as s:
        rows = {c.slug: c for c in (await s.scalars(select(Company))).all()}
        assert await s.scalar(select(func.count()).select_from(Posting)) == 0  # verification never writes postings
    for slug in ("acme", "delta", "zeta"):
        assert rows[slug].last_verify_status == "ok" and rows[slug].last_verify_error is None
        assert rows[slug].last_verified_at >= before
    for slug in ("beta", "gamma", "epsilon"):
        assert rows[slug].last_verify_status == "error" and rows[slug].last_verify_error == results[slug].error
        assert rows[slug].last_verified_at >= before
    dormant = rows["dormant"]
    assert (dormant.last_verified_at, dormant.last_verify_status, dormant.last_verify_error) == (None, None, None)


async def test_verify_sources_clears_old_error_on_success_and_filters_by_source(db: Database):
    await _add_companies(db)
    async with db.session() as s:
        acme = await s.scalar(select(Company).where(Company.slug == "acme"))
        acme.last_verify_status, acme.last_verify_error = "error", "HTTP 404 (yesterday)"
        await s.commit()
    seen: list[str] = []

    async with make_client(seen, []) as client:
        report = await verify_sources(db, client, source="greenhouse")

    assert [r.slug for r in report.results] == ["acme"]
    assert seen == ["boards-api.greenhouse.io/v1/boards/acme/jobs"]
    async with db.session() as s:
        acme = await s.scalar(select(Company).where(Company.slug == "acme"))
        beta = await s.scalar(select(Company).where(Company.slug == "beta"))
    assert (acme.last_verify_status, acme.last_verify_error) == ("ok", None)
    assert beta.last_verified_at is None  # other sources untouched

    async with make_client([], []) as client:
        with pytest.raises(ValueError, match="unknown source 'workday'"):
            await verify_sources(db, client, source="workday")


def test_count_titles_and_report_format():
    assert count_titles(["SWE Intern", "Co-op Developer", "Coop student", "Internship", "Interns", "Internal Audit",
                         "New Graduate Engineer", "new-grad SWE", "Early Career PM"]) == (5, 3)

    report = VerifyReport(
        [
            VerifyResult("acme", "greenhouse", "acme", ok=True, postings=5, intern=2, new_grad=1),
            VerifyResult("beta", "lever", "beta", ok=False, error="GET https://x returned HTTP 404"),
        ]
    )
    text = format_report(report, min_ok=2)
    lines = text.splitlines()
    assert lines[0].split() == ["slug", "source", "status", "postings", "intern", "newgrad", "error"]
    assert lines[2].split() == ["acme", "greenhouse", "ok", "5", "2", "1"]
    assert lines[3].split()[:6] == ["beta", "lever", "ERROR", "0", "0", "0"] and "HTTP 404" in lines[3]
    assert ["greenhouse", "1", "1", "0", "5", "2", "1"] in [ln.split() for ln in lines]
    assert ["lever", "1", "0", "1", "0", "0", "0"] in [ln.split() for ln in lines]
    assert lines[-1] == "FAIL: 1/2 companies OK (need at least 2)"
    assert format_report(report, min_ok=1).splitlines()[-1] == "PASS: 1/2 companies OK (need at least 1)"


# ---- CLI ----------------------------------------------------------------------------------------


def _cli_setup(monkeypatch, database_url: str, tmp_path: Path) -> list[str]:
    async def _reset() -> None:
        await sql(database_url, "TRUNCATE radar_companies RESTART IDENTITY CASCADE")
        database = Database(database_url)
        await database.connect()
        try:
            await _add_companies(database)
        finally:
            await database.close()

    asyncio.run(_reset())
    patch_runtime(monkeypatch, database_url, tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(radar_cli, "make_http_client", lambda: make_client(seen, []))
    return seen


def test_cli_verify_sources_exit_code_follows_threshold(monkeypatch, tmp_path: Path, database_url: str):
    seen = _cli_setup(monkeypatch, database_url, tmp_path)
    runner = CliRunner()

    below = runner.invoke(app, ["radar", "verify-sources"])  # default threshold 20, only 3 pass
    enough = runner.invoke(app, ["radar", "verify-sources", "--min-ok", "3"])

    assert below.exit_code == 1, below.output
    assert "FAIL: 3/6 companies OK (need at least 20)" in below.output
    assert "acme" in below.output and "HTTP 404" in below.output
    assert enough.exit_code == 0, enough.output
    assert "PASS: 3/6 companies OK (need at least 3)" in enough.output
    assert not any("dormant" in u for u in seen)
    statuses = asyncio.run(sql(database_url, "SELECT slug, last_verify_status FROM radar_companies ORDER BY slug"))
    assert dict(statuses) == {"acme": "ok", "beta": "error", "delta": "ok", "dormant": None, "epsilon": "error",
                              "gamma": "error", "zeta": "ok"}


def test_cli_verify_sources_single_source(monkeypatch, tmp_path: Path, database_url: str):
    _cli_setup(monkeypatch, database_url, tmp_path)
    runner = CliRunner()

    lever = runner.invoke(app, ["radar", "verify-sources", "--source", "lever"])  # threshold defaults to 1
    greenhouse = runner.invoke(app, ["radar", "verify-sources", "--source", "greenhouse"])
    unknown = runner.invoke(app, ["radar", "verify-sources", "--source", "workday"])

    assert lever.exit_code == 1 and "FAIL: 0/1 companies OK (need at least 1)" in lever.output
    assert greenhouse.exit_code == 0 and "PASS: 1/1 companies OK (need at least 1)" in greenhouse.output
    assert unknown.exit_code == 1 and "unknown source 'workday'" in unknown.output
