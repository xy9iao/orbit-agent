"""`orbit radar poll` (M3.4) and the `orbit radar resume` default path, through CliRunner.

Boards are served by test_poll's FakeBoards on httpx.MockTransport (never the network); the runtime is
monkeypatched onto the test database like the other radar CLI tests.
"""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import orbit.radar.cli as radar_cli
from orbit.cli.main import app
from orbit.config import Settings
from orbit.core.router import ModelRouter
from orbit.radar.cli import default_resume_path
from orbit.radar.profile import DEFAULT_RESUME_PATH
from orbit.storage.db import Database
from orbit.storage.models import Company
from tests.conftest import FakeProvider, make_config
from tests.radar.test_poll import FakeBoards, acme_payload, faire_payload, make_client
from tests.radar.test_seed import patch_runtime, sql


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any request that escapes a MockTransport fails loudly instead of reaching the internet."""

    async def _refuse(self, request):
        raise AssertionError(f"test tried to reach the network: {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _refuse)


# (slug, source, active); board tokens equal slugs
COMPANIES = [
    ("acme", "greenhouse", True),
    ("faire", "greenhouse", True),
    ("broken", "greenhouse", True),  # FakeBoards answers 404 for unknown tokens
    ("dormant", "greenhouse", False),
]


def _setup(monkeypatch, database_url: str, tmp_path: Path, boards: FakeBoards, companies=COMPANIES) -> None:
    async def _reset() -> None:
        await sql(database_url, "TRUNCATE radar_companies RESTART IDENTITY CASCADE")
        database = Database(database_url)
        await database.connect()
        try:
            async with database.session() as s:
                for slug, source, active in companies:
                    s.add(Company(slug=slug, name=slug.title(), source=source, board_token=slug, active=active))
                await s.commit()
        finally:
            await database.close()

    asyncio.run(_reset())
    patch_runtime(monkeypatch, database_url, tmp_path)  # a router with no providers: polling needs no LLM key
    monkeypatch.setattr(radar_cli, "make_http_client", lambda: make_client(boards))


def _rows(output: str) -> dict[str, list[str]]:
    """Per-company table rows of the poll output, keyed by slug."""
    lines = output.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.split()[:2] == ["slug", "status"])
    return {ln.split()[0]: ln.split() for ln in lines[start + 1 :] if not ln.startswith("total:")}


def _total(output: str) -> str:
    return next(ln for ln in output.splitlines() if ln.startswith("total:"))


def test_cli_poll_prints_per_company_lines_and_writes_postings_and_runs(monkeypatch, tmp_path, database_url):
    boards = FakeBoards(acme=acme_payload(), faire=faire_payload())
    _setup(monkeypatch, database_url, tmp_path, boards)
    n = len(faire_payload()["jobs"])
    runner = CliRunner()

    first = runner.invoke(app, ["radar", "poll"])

    assert first.exit_code == 0, first.output  # one company failed, the others succeeded
    assert sorted(boards.tokens_requested()) == ["acme", "broken", "faire"]  # inactive "dormant" skipped
    rows = _rows(first.output)
    assert list(rows) == ["acme", "broken", "faire"]
    assert rows["acme"] == ["acme", "ok", "1", "1", "0", "0", "0"]
    assert rows["faire"] == ["faire", "ok", str(n), str(n), "0", "0", "0"]
    assert rows["broken"][:7] == ["broken", "ERROR", "0", "0", "0", "0", "0"]
    assert "HTTP 404" in " ".join(rows["broken"][7:])
    assert _total(first.output).startswith(
        f"total: 3 companies, 2 ok, 1 failed; fetched {n + 1}, new {n + 1}, updated 0, closed 0, reopened 0 ("
    )
    assert asyncio.run(sql(database_url, "SELECT count(*) FROM radar_postings WHERE status = 'open'")) == [(n + 1,)]
    runs = asyncio.run(
        sql(
            database_url,
            "SELECT c.slug, r.ok, r.fetched, r.new, r.error IS NOT NULL FROM radar_runs r "
            "JOIN radar_companies c ON c.id = r.company_id ORDER BY c.slug",
        )
    )
    assert runs == [("acme", True, 1, 1, False), ("broken", False, 0, 0, True), ("faire", True, n, n, False)]

    # Second poll: the acme posting disappeared and one faire posting changed.
    boards.boards["acme"] = {"jobs": [], "meta": {"total": 0}}
    changed = copy.deepcopy(faire_payload())
    changed["jobs"][0]["title"] += " (updated)"
    boards.boards["faire"] = changed

    second = runner.invoke(app, ["radar", "poll"])

    assert second.exit_code == 0, second.output
    rows = _rows(second.output)
    assert rows["acme"] == ["acme", "ok", "0", "0", "0", "1", "0"]
    assert rows["faire"] == ["faire", "ok", str(n), "0", "1", "0", "0"]
    assert "new 0, updated 1, closed 1, reopened 0" in _total(second.output)
    assert asyncio.run(sql(database_url, "SELECT count(*) FROM radar_runs")) == [(6,)]


def test_cli_poll_company_and_source_filters(monkeypatch, tmp_path, database_url):
    boards = FakeBoards(acme=acme_payload(), faire=faire_payload())
    companies = [*COMPANIES, ("lev", "lever", True)]
    _setup(monkeypatch, database_url, tmp_path, boards, companies)
    runner = CliRunner()

    one = runner.invoke(app, ["radar", "poll", "--company", "acme"])
    assert one.exit_code == 0, one.output
    assert boards.tokens_requested() == ["acme"]
    assert list(_rows(one.output)) == ["acme"]
    assert _total(one.output).startswith("total: 1 companies, 1 ok, 0 failed; fetched 1, new 1,")

    boards.requests.clear()
    two = runner.invoke(app, ["radar", "poll", "-c", "faire", "-c", "acme", "--source", "greenhouse"])
    assert two.exit_code == 0, two.output
    assert sorted(boards.tokens_requested()) == ["acme", "faire"]
    assert list(_rows(two.output)) == ["acme", "faire"]

    boards.requests.clear()
    by_source = runner.invoke(app, ["radar", "poll", "--source", "greenhouse"])
    assert by_source.exit_code == 0, by_source.output
    assert sorted(boards.tokens_requested()) == ["acme", "broken", "faire"]  # never the lever board
    assert "lev" not in _rows(by_source.output)

    boards.requests.clear()
    unknown = runner.invoke(app, ["radar", "poll", "--company", "acme", "--company", "nope"])
    inactive = runner.invoke(app, ["radar", "poll", "--company", "dormant"])
    bad_source = runner.invoke(app, ["radar", "poll", "--source", "workday"])
    no_match = runner.invoke(app, ["radar", "poll", "--source", "ashby"])
    assert unknown.exit_code == 1 and "not an active radar company: nope" in unknown.output
    assert inactive.exit_code == 1 and "not an active radar company: dormant" in inactive.output
    assert bad_source.exit_code == 1 and "unknown source 'workday'" in bad_source.output
    assert no_match.exit_code == 0 and "no active ashby companies to poll" in no_match.output
    assert boards.requests == []  # nothing was fetched for any of these


def test_cli_poll_exit_code_1_when_every_company_fails(monkeypatch, tmp_path, database_url):
    boards = FakeBoards(acme=500, faire=404)
    _setup(monkeypatch, database_url, tmp_path, boards)

    result = CliRunner().invoke(app, ["radar", "poll", "-c", "acme", "-c", "faire"])

    assert result.exit_code == 1, result.output
    rows = _rows(result.output)
    assert [r[1] for r in rows.values()] == ["ERROR", "ERROR"]
    assert _total(result.output).startswith("total: 2 companies, 0 ok, 2 failed; fetched 0, new 0,")
    assert asyncio.run(sql(database_url, "SELECT ok, count(*) FROM radar_runs GROUP BY ok")) == [(False, 2)]
    assert asyncio.run(sql(database_url, "SELECT count(*) FROM radar_postings")) == [(0,)]


def test_cli_poll_empty_radar_hints_seed(monkeypatch, tmp_path, database_url):
    boards = FakeBoards()
    _setup(monkeypatch, database_url, tmp_path, boards, companies=[("dormant", "greenhouse", False)])

    result = CliRunner().invoke(app, ["radar", "poll"])

    assert result.exit_code == 0, result.output
    assert "run `orbit radar seed` first" in result.output
    assert boards.requests == []
    assert asyncio.run(sql(database_url, "SELECT count(*) FROM radar_runs")) == [(0,)]


# ---- resume default path (radar.resume_path in config.yaml) -----------------------------------------

RESUME = "# Xinyang\n\nWaterloo CS 3A. Python, Postgres.\n"


def _patch_resume_runtime(monkeypatch, database_url: str, home: Path, resume_path: str, providers: dict) -> None:
    @asynccontextmanager
    async def fake_runtime(settings=None):
        database = Database(database_url)
        await database.connect()
        try:
            config = make_config()
            config.radar.resume_path = resume_path
            router = ModelRouter(config, providers, usage_sink=database.add_usage)
            yield radar_cli.RadarRuntime(Settings(orbit_home=home), config, database, router)
        finally:
            await database.close()

    monkeypatch.setattr(radar_cli, "open_runtime", fake_runtime)


def test_default_resume_path_comes_from_config(tmp_path: Path):
    config = make_config()
    settings = Settings(orbit_home=tmp_path)
    assert default_resume_path(settings, config) == tmp_path / DEFAULT_RESUME_PATH  # unchanged default
    config.radar.resume_path = "cv/me.txt"
    assert default_resume_path(settings, config) == tmp_path / "cv" / "me.txt"
    absolute = tmp_path / "elsewhere" / "resume.md"
    config.radar.resume_path = str(absolute)
    assert default_resume_path(settings, config) == absolute


def test_cli_resume_without_path_uses_configured_resume_path(monkeypatch, tmp_path: Path, database_url: str):
    asyncio.run(sql(database_url, "TRUNCATE radar_profile, usage RESTART IDENTITY"))
    fake = FakeProvider("cheap")
    _patch_resume_runtime(monkeypatch, database_url, tmp_path, "cv/me.txt", {"cheap": fake})
    runner = CliRunner()

    missing = runner.invoke(app, ["radar", "resume"])
    assert missing.exit_code == 1
    assert "not found" in missing.output and "me.txt" in missing.output
    assert fake.embed_calls == []

    configured = tmp_path / "cv" / "me.txt"
    configured.parent.mkdir()
    configured.write_text(RESUME, encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / DEFAULT_RESUME_PATH).write_text("the old default location, not configured", encoding="utf-8")

    result = runner.invoke(app, ["radar", "resume"])

    assert result.exit_code == 0, result.output
    assert f"resume embedded: {len(RESUME.strip())} chars" in result.output
    assert str(configured) in result.output
    assert fake.embed_calls[0]["texts"] == [RESUME.strip()]
    assert asyncio.run(sql(database_url, "SELECT kind, left(text, 9) FROM radar_profile")) == [("resume", "# Xinyang")]


def test_cli_resume_default_path_without_key_fails_cleanly(monkeypatch, tmp_path: Path, database_url: str):
    _patch_resume_runtime(monkeypatch, database_url, tmp_path, "resume.txt", {})  # no provider for 'embed'
    (tmp_path / "resume.txt").write_text(RESUME, encoding="utf-8")

    result = CliRunner().invoke(app, ["radar", "resume"])

    assert result.exit_code == 1 and "OPENAI_API_KEY" in result.output
