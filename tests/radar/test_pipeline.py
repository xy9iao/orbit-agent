"""The radar pipeline (M4.3): poll -> extract, stage isolation, failed-extraction retry, watcher and CLI.

Boards come from test_poll's FakeBoards on httpx.MockTransport and the LLM is FakeProvider; nothing
reaches the network.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from loguru import logger
from sqlalchemy import select
from typer.testing import CliRunner

import orbit.radar.cli as radar_cli
from orbit.cli.main import app
from orbit.config import RadarConfig, Settings
from orbit.core.router import ModelRouter
from orbit.radar import pipeline as pl
from orbit.radar.extract import ExtractSummary
from orbit.radar.pipeline import PipelineSummary, missing_provider, retry_failed_extractions, run_pipeline
from orbit.radar.watcher import InternshipRadarWatcher
from orbit.storage.db import Database
from orbit.storage.models import Company, Posting
from tests.conftest import FakeProvider, make_config
from tests.radar.test_extract import GOOD, RaisingProvider, make_router, reply
from tests.radar.test_poll import FakeBoards, acme_payload, faire_payload, make_client, no_network  # noqa: F401
from tests.radar.test_seed import sql
from tests.test_health import make_app

NOW = datetime(2026, 9, 2, 12, 0, 0)
FAIRE_JOBS = len(faire_payload()["jobs"])  # 5, one of them ("Director, ...") is prefiltered


@pytest.fixture
def warnings():
    """Loguru WARNING+ messages emitted during the test."""
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    yield messages
    logger.remove(sink)


async def _company(db: Database, slug: str = "acme", *, active: bool = True) -> Company:
    async with db.session() as s:
        company = Company(slug=slug, name=slug.title(), source="greenhouse", board_token=slug, active=active)
        s.add(company)
        await s.commit()
        return company


async def _rows(db: Database) -> dict[str, Posting]:
    async with db.session() as s:
        return {p.external_id: p for p in (await s.execute(select(Posting))).scalars()}


async def _run(db: Database, router: ModelRouter | None, boards: FakeBoards, **config: Any) -> PipelineSummary:
    async with make_client(boards) as client:
        return await run_pipeline(db, router, client, RadarConfig(**config), now=NOW)


# ---- poll -> extract --------------------------------------------------------------------------------


async def test_pipeline_polls_then_extracts(db: Database):
    await _company(db)
    fake = FakeProvider("cheap", [reply(GOOD)])

    summary = await _run(db, make_router(fake), FakeBoards(acme=acme_payload()))

    assert summary.ok and summary.errors == {} and summary.skipped == {}
    assert summary.poll.new == 1
    assert summary.extract == ExtractSummary(ok=1)
    [row] = (await _rows(db)).values()
    assert row.extraction_status == "ok" and row.extraction["is_internship"] is True
    assert row.extracted_at == NOW
    assert "Software Developer Co-op" in fake.calls[0]["messages"][1]["content"]
    assert "extract: ok 1, failed 0, skipped 0, stale 0" in pl.describe(summary)


async def test_extract_cap_is_respected(db: Database):
    await _company(db, "faire")
    fake = FakeProvider("cheap", [reply(GOOD)] * FAIRE_JOBS)

    summary = await _run(db, make_router(fake), FakeBoards(faire=faire_payload()), max_extract_per_poll=2)

    assert summary.poll.new == FAIRE_JOBS
    assert summary.extract.processed == 2
    statuses = [p.extraction_status for p in (await _rows(db)).values()]
    assert statuses.count("pending") == FAIRE_JOBS - 2


async def test_zero_cap_extracts_nothing(db: Database):
    await _company(db)
    fake = FakeProvider("cheap")

    summary = await _run(db, make_router(fake), FakeBoards(acme=acme_payload()), max_extract_per_poll=0)

    assert summary.extract == ExtractSummary() and fake.calls == []


async def test_no_provider_polls_and_skips_extraction_with_one_warning(db: Database, warnings):
    await _company(db)
    router = ModelRouter(make_config(), {})  # no API keys: no providers at all

    summary = await _run(db, router, FakeBoards(acme=acme_payload()))

    assert summary.ok  # skipping is not an error
    assert summary.poll.new == 1 and summary.extract is None
    assert summary.skipped == {"extract": "no provider for task 'extract' — set OPENAI_API_KEY"}
    assert [w for w in warnings if "extraction skipped" in w] == [
        "radar pipeline: extraction skipped: no provider for task 'extract' — set OPENAI_API_KEY"
    ]
    [row] = (await _rows(db)).values()
    assert row.extraction_status == "pending"
    assert "extract: skipped (no provider" in pl.describe(summary)


def test_missing_provider_hints():
    config = make_config()
    assert missing_provider(ModelRouter(config, {"cheap": FakeProvider("cheap")}), "extract") is None
    assert missing_provider(None, "extract") == "no model router for task 'extract'"
    del config.tasks["chat"], config.tasks["extract"]
    assert "no 'extract' task" in missing_provider(ModelRouter(config, {}), "extract")


async def test_provider_errors_do_not_abort_the_run(db: Database):
    await _company(db, "faire")
    fake = RaisingProvider("cheap")

    summary = await _run(db, make_router(fake), FakeBoards(faire=faire_payload()))

    assert summary.ok and summary.poll.ok == 1 and summary.poll.new == FAIRE_JOBS
    assert summary.extract.aborted and summary.extract.failed == 3
    assert summary.retried is None  # no retries while the provider is failing
    statuses = [p.extraction_status for p in (await _rows(db)).values()]
    assert statuses.count("failed") == 3


async def test_a_raising_stage_is_recorded_and_later_stages_still_run(db: Database, monkeypatch):
    await _company(db)

    async def broken_poll(*_a, **_kw):
        raise ConnectionError("database went away\nmore detail")

    real_poll_all = pl.poll_all
    monkeypatch.setattr(pl, "poll_all", broken_poll)
    fake = FakeProvider("cheap")

    summary = await _run(db, make_router(fake), FakeBoards(acme=acme_payload()))

    assert summary.errors == {"poll": "ConnectionError: database went away"}
    assert summary.poll is None and summary.extract == ExtractSummary()  # extract ran (nothing pending)
    assert not summary.ok and "poll: ERROR ConnectionError" in pl.describe(summary)

    async def broken_extract(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(pl, "poll_all", real_poll_all)  # not undo(): that would also drop no_network
    monkeypatch.setattr(pl.ex, "extract_pending", broken_extract)

    summary = await _run(db, make_router(fake), FakeBoards(acme=acme_payload()))

    assert summary.poll.new == 1 and summary.errors == {"extract": "RuntimeError: boom"}


# ---- automatic retry of failed extractions ----------------------------------------------------------


async def _failed(db: Database, company: Company, external_id: str, *, hours_ago: float, error: str) -> None:
    async with db.session() as s:
        s.add(
            Posting(
                source="greenhouse",
                external_id=external_id,
                company_id=company.id,
                title="Software Engineering Intern",
                url=f"https://example.com/{external_id}",
                location_raw="Toronto, ON",
                department=None,
                description_text="Build things.",
                raw={},
                content_hash=f"hash-{external_id}",
                first_seen_at=NOW - timedelta(days=1),
                last_seen_at=NOW,
                extraction_status="failed",
                extraction_error=error,
                extracted_at=NOW - timedelta(hours=hours_ago),
            )
        )
        await s.commit()


PROVIDER_ERROR = "ConnectionError: connection reset by peer"
INVALID_REPLY = "after retry: ValidationError: terms.0: bad term"


async def test_retry_only_old_provider_failures_with_a_cap(db: Database):
    company = await _company(db)
    for i in range(4):
        await _failed(db, company, f"old{i}", hours_ago=10 + i, error=PROVIDER_ERROR)
    await _failed(db, company, "recent", hours_ago=1, error=PROVIDER_ERROR)
    await _failed(db, company, "invalid", hours_ago=24, error=INVALID_REPLY)
    fake = FakeProvider("cheap", [reply(GOOD)] * 3)

    summary = await retry_failed_extractions(db, make_router(fake), now=NOW)

    assert summary == ExtractSummary(ok=pl.RETRY_FAILED_PER_POLL)
    statuses = {k: p.extraction_status for k, p in (await _rows(db)).items()}
    # Oldest attempt first: old3 (13h), old2, old1 retried; old0 waits for the next run.
    assert statuses == {
        "old0": "failed", "old1": "ok", "old2": "ok", "old3": "ok", "recent": "failed", "invalid": "failed",
    }  # fmt: skip


async def test_retry_stops_at_the_first_provider_error(db: Database):
    company = await _company(db)
    for i in range(3):
        await _failed(db, company, f"old{i}", hours_ago=10 + i, error=PROVIDER_ERROR)
    fake = RaisingProvider("cheap")

    summary = await retry_failed_extractions(db, make_router(fake), now=NOW)

    assert summary == ExtractSummary(failed=1, aborted=True) and len(fake.calls) == 1
    rows = await _rows(db)
    assert rows["old2"].extracted_at == NOW  # its retry clock restarts
    assert rows["old0"].extracted_at == NOW - timedelta(hours=10)


async def test_pipeline_runs_the_retry_after_pending_extraction(db: Database):
    await _company(db)
    # On a board this run doesn't poll; on acme's board a posting missing from the fetch would be closed.
    other = await _company(db, "other", active=False)
    await _failed(db, other, "old", hours_ago=7, error=PROVIDER_ERROR)
    fake = FakeProvider("cheap", [reply(GOOD)] * 2)

    summary = await _run(db, make_router(fake), FakeBoards(acme=acme_payload()))

    assert summary.extract == ExtractSummary(ok=1) and summary.retried == ExtractSummary(ok=1)
    assert {p.extraction_status for p in (await _rows(db)).values()} == {"ok"}
    assert "retried failed: ok 1" in pl.describe(summary)


# ---- watcher ----------------------------------------------------------------------------------------


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_watcher_check_runs_the_pipeline_with_the_app_router(tmp_path: Path, db: Database):
    await _company(db)
    boards = FakeBoards(acme=acme_payload())
    orbit = make_app(tmp_path, db)
    fake = FakeProvider("cheap", [reply(GOOD)])
    orbit.router = make_router(fake)
    orbit.config.radar.max_extract_per_poll = 5
    watcher = InternshipRadarWatcher(client_factory=lambda: make_client(boards))
    watcher.bind(orbit)
    try:
        events = await watcher.check(db)
    finally:
        await watcher.close()

    assert events == []
    assert boards.tokens_requested() == ["acme"]
    [row] = (await _rows(db)).values()
    assert row.extraction_status == "ok" and len(fake.calls) == 1


async def test_unbound_watcher_polls_and_skips_extraction(db: Database):
    await _company(db)
    boards = FakeBoards(acme=acme_payload())
    watcher = InternshipRadarWatcher(client_factory=lambda: make_client(boards))
    try:
        assert await watcher.check(db) == []
    finally:
        await watcher.close()
    [row] = (await _rows(db)).values()
    assert row.extraction_status == "pending"


# ---- `orbit radar extract` --------------------------------------------------------------------------


def _patch_runtime(monkeypatch, database_url: str, tmp_path: Path, providers: dict, **radar: Any) -> None:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_runtime(settings=None):
        database = Database(database_url)
        await database.connect()
        try:
            config = make_config()
            config.radar = RadarConfig(**radar)
            router = ModelRouter(config, providers, usage_sink=database.add_usage)
            yield radar_cli.RadarRuntime(Settings(orbit_home=tmp_path), config, database, router)
        finally:
            await database.close()

    monkeypatch.setattr(radar_cli, "open_runtime", fake_runtime)


def _seed_postings(database_url: str, statuses: list[str]) -> None:
    async def _go() -> None:
        await sql(database_url, "TRUNCATE radar_companies RESTART IDENTITY CASCADE")
        database = Database(database_url)
        await database.connect()
        try:
            company = await _company(database)
            for i, status in enumerate(statuses):
                async with database.session() as s:
                    s.add(
                        Posting(
                            source="greenhouse",
                            external_id=str(i),
                            company_id=company.id,
                            title="Software Engineering Intern",
                            url=f"https://example.com/{i}",
                            location_raw="Toronto, ON",
                            department=None,
                            description_text="Build things.",
                            raw={},
                            content_hash=f"hash-{i}",
                            first_seen_at=NOW + timedelta(minutes=i),
                            last_seen_at=NOW,
                            extraction_status=status,
                            extraction_error=PROVIDER_ERROR if status == "failed" else None,
                        )
                    )
                    await s.commit()
        finally:
            await database.close()

    asyncio.run(_go())


def _statuses(database_url: str) -> list[str]:
    rows = asyncio.run(sql(database_url, "SELECT extraction_status FROM radar_postings ORDER BY external_id"))
    return [r[0] for r in rows]


def test_cli_extract_without_provider_fails_cleanly(monkeypatch, tmp_path: Path, database_url: str):
    _seed_postings(database_url, ["pending"])
    _patch_runtime(monkeypatch, database_url, tmp_path, {})

    result = CliRunner().invoke(app, ["radar", "extract"])

    assert result.exit_code == 1
    assert "no provider for the 'extract' task" in result.output and "OPENAI_API_KEY" in result.output
    assert _statuses(database_url) == ["pending"]


def test_cli_extract_limit_and_counts(monkeypatch, tmp_path: Path, database_url: str):
    _seed_postings(database_url, ["pending", "pending", "pending", "failed"])
    fake = FakeProvider("cheap", [reply(GOOD)] * 4)
    _patch_runtime(monkeypatch, database_url, tmp_path, {"cheap": fake})
    runner = CliRunner()

    result = runner.invoke(app, ["radar", "extract", "--limit", "2"])

    assert result.exit_code == 0, result.output
    assert "extract: ok 2, failed 0, skipped 0, stale 0" in result.output
    assert "1 open postings still pending (run again to continue)" in result.output
    assert _statuses(database_url) == ["pending", "ok", "ok", "failed"]  # newest first

    result = runner.invoke(app, ["radar", "extract", "--retry-failed"])

    assert result.exit_code == 0, result.output
    assert "extract: ok 2, failed 0" in result.output
    assert "0 open postings still pending or failed" in result.output
    assert _statuses(database_url) == ["ok", "ok", "ok", "ok"]


def test_cli_extract_default_limit_is_max_extract_per_poll(monkeypatch, tmp_path: Path, database_url: str):
    _seed_postings(database_url, ["pending", "pending", "pending"])
    fake = FakeProvider("cheap", [reply(GOOD)] * 3)
    _patch_runtime(monkeypatch, database_url, tmp_path, {"cheap": fake}, max_extract_per_poll=1)

    result = CliRunner().invoke(app, ["radar", "extract"])

    assert result.exit_code == 0, result.output
    assert "extract: ok 1," in result.output and "2 open postings still pending" in result.output


def test_cli_extract_exit_1_after_provider_errors(monkeypatch, tmp_path: Path, database_url: str):
    _seed_postings(database_url, ["pending"] * 4)
    _patch_runtime(monkeypatch, database_url, tmp_path, {"cheap": RaisingProvider("cheap")})

    result = CliRunner().invoke(app, ["radar", "extract"])

    assert result.exit_code == 1
    assert "failed 3" in result.output and "aborted" in result.output
    assert "stopped after repeated provider errors" in result.output


def test_cli_extract_rejects_non_positive_limit():
    result = CliRunner().invoke(app, ["radar", "extract", "--limit", "0"])
    assert result.exit_code == 2
