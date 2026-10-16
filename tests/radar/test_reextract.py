"""`orbit radar reextract`: re-extraction of ok postings through a message batch (fake batch provider)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from typer.testing import CliRunner

from orbit.cli.main import app
from orbit.core.providers.base import Response
from orbit.core.router import ModelRouter
from orbit.radar.extract import SYSTEM_PROMPT
from orbit.radar.reextract import KV_KEY, collect, custom_id, describe, select_extracted, submit
from orbit.storage.db import Database
from orbit.storage.models import Notification, Posting
from tests.conftest import FakeProvider, make_config
from tests.radar.test_extract import GOOD_NORMALIZED
from tests.radar.test_match import SKIPPED, _add, _company, _row, _rows
from tests.radar.test_pipeline import _patch_runtime
from tests.radar.test_seed import sql

NEW = {**GOOD_NORMALIZED, "role_type": "swe", "locations": ["Waterloo, ON"], "tech_stack": ["go"]}


class FakeBatchProvider(FakeProvider):
    """A FakeProvider that also answers the batch calls: `answer(posting_id)` gives each result."""

    def __init__(self, name: str, answer=None, status: str = "ended"):
        super().__init__(name)
        self.answer = answer or (lambda pid: json.dumps(NEW))
        self.status = status
        self.submitted: list[dict[str, Any]] = []

    async def submit_batch(self, model, requests, **params):
        self.submitted.append({"model": model, "requests": list(requests), "params": params})
        return f"msgbatch_{len(self.submitted)}"

    async def batch_status(self, batch_id):
        return self.status

    async def batch_results(self, batch_id):
        out: dict[str, Response | str] = {}
        for cid, _ in self.submitted[-1]["requests"]:
            text = self.answer(int(cid.removeprefix("posting-")))
            if text is None:
                out[cid] = "errored: overloaded"
            else:
                usage = {"prompt_tokens": 100, "completion_tokens": 20, "cache_read_input_tokens": 80}
                out[cid] = Response(text=text, usage=usage)
        return out


def _router(provider: FakeProvider) -> ModelRouter:
    return ModelRouter(make_config(), {"cheap": provider, "openai": FakeProvider("openai")})


async def _seed(db: Database) -> None:
    company = await _company(db)
    await _add(
        db,
        _row(company, "a", minutes=3, embedded_hash="hash-a"),
        _row(company, "b", minutes=2, embedded_hash="hash-b"),
        _row(company, "c", minutes=1, embedded_hash="hash-c"),
        _row(company, "skipped", extraction_status="skipped", extraction=SKIPPED),
        _row(company, "closed", status="closed"),
        _row(company, "pending", extraction_status="pending", extraction=None),
    )


async def test_select_extracted_takes_only_open_ok_postings_newest_first(db: Database):
    await _seed(db)
    assert [p.title[-1] for p in await select_extracted(db)] == ["a", "b", "c"]
    assert len(await select_extracted(db, limit=2)) == 2


async def test_submit_sends_one_request_per_posting_and_remembers_the_batch(db: Database):
    await _seed(db)
    provider = FakeBatchProvider("cheap")
    postings = await select_extracted(db)

    state = await submit(db, _router(provider), postings)

    sent = provider.submitted[0]
    assert sent["model"] == "extract-small"
    assert [cid for cid, _ in sent["requests"]] == [custom_id(p.id) for p in postings]
    messages = sent["requests"][0][1]
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert "Software Engineering Intern a" in messages[1]["content"]
    assert sent["params"]["response_format"]["type"] == "json_schema"
    assert state["batch_id"] == "msgbatch_1" and state["model"] == "extract-small"
    assert state["postings"] == {str(p.id): p.content_hash for p in postings}
    assert await db.kv_get(KV_KEY) == state


async def test_collect_writes_valid_results_keeps_old_on_failure_and_skips_changed_postings(db: Database):
    await _seed(db)
    rows = await _rows(db)
    ids = {key: rows[key].id for key in "abc"}
    answers = {ids["a"]: json.dumps(NEW), ids["b"]: "not json", ids["c"]: json.dumps(NEW)}
    provider = FakeBatchProvider("cheap", answer=answers.get)
    router = _router(provider)
    state = await submit(db, router, await select_extracted(db))
    async with db.session() as s:  # a poll changed c while the batch ran
        await s.execute(update(Posting).where(Posting.id == ids["c"]).values(content_hash="hash-c2"))
        await s.commit()

    summary = await collect(db, router, state)

    assert (summary.ok, summary.failed, summary.stale) == (1, 1, 1)
    assert (summary.changed_role_type, summary.changed_locations, summary.reembed) == (1, 1, 1)
    assert summary.usage == {
        "prompt_tokens": 300,
        "completion_tokens": 60,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 240,
    }
    rows = await _rows(db)
    assert rows["a"].extraction == NEW and rows["a"].extraction_model == "extract-small"
    assert rows["a"].embedded_hash is None  # embedding text changed: re-embed
    assert rows["b"].extraction == GOOD_NORMALIZED and rows["b"].embedded_hash == "hash-b"
    assert rows["c"].extraction == GOOD_NORMALIZED
    assert await db.kv_get(KV_KEY) is None
    assert "1 re-extracted, 1 failed" in describe(summary) and "role_type 1, locations 1" in describe(summary)


async def test_collect_keeps_the_embedding_when_embedded_fields_are_unchanged(db: Database):
    await _seed(db)
    provider = FakeBatchProvider("cheap", answer=lambda pid: json.dumps({**GOOD_NORMALIZED, "remote_ok": True}))
    router = _router(provider)
    state = await submit(db, router, await select_extracted(db, limit=1))

    summary = await collect(db, router, state)

    assert (summary.ok, summary.changed_role_type, summary.changed_locations, summary.reembed) == (1, 0, 0, 0)
    row = (await _rows(db))["a"]
    assert row.extraction["remote_ok"] is True and row.embedded_hash == "hash-a"


async def test_collect_counts_errored_requests_as_failed(db: Database):
    await _seed(db)
    router = _router(FakeBatchProvider("cheap", answer=lambda pid: None))
    state = await submit(db, router, await select_extracted(db))

    summary = await collect(db, router, state)

    assert (summary.ok, summary.failed) == (0, 3)
    assert all(row.extraction == GOOD_NORMALIZED for key, row in (await _rows(db)).items() if key in "abc")


# ---- CLI ---------------------------------------------------------------------------------------------


def _reset(database_url: str) -> None:
    async def go() -> None:
        db = Database(database_url)
        await db.connect()
        try:
            tables = "radar_companies, radar_postings, kv, radar_notifications, usage"
            await sql(database_url, f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
            await _seed(db)
            async with db.session() as s:  # posting a was notified before; rescoring must not change that
                a = (await s.execute(select(Posting).where(Posting.external_id == "a"))).scalar_one()
                s.add(Notification(posting_id=a.id, score=0.6, status="sent"))
                await s.commit()
        finally:
            await db.close()

    asyncio.run(go())


def test_cli_reextract_waits_collects_embeds_and_scores(monkeypatch, tmp_path: Path, database_url: str):
    _reset(database_url)
    provider = FakeBatchProvider("cheap")
    _patch_runtime(monkeypatch, database_url, tmp_path, {"cheap": provider, "openai": FakeProvider("openai")})

    result = CliRunner().invoke(app, ["radar", "reextract"])

    assert result.exit_code == 0, result.output
    assert "submitted batch msgbatch_1: 3 postings to extract-small" in result.output
    assert "reextract: 3 re-extracted, 0 failed" in result.output
    assert "role_type 3, locations 3" in result.output
    assert "embed: embedded 3, failed 0" in result.output
    assert "score:" in result.output
    assert asyncio.run(sql(database_url, "SELECT count(*) FROM radar_notifications")) == [(1,)]
    assert asyncio.run(sql(database_url, "SELECT count(*) FROM usage WHERE task = 'extract'")) == [(3,)]


def test_cli_reextract_no_wait_then_resume_collects_the_same_batch(monkeypatch, tmp_path: Path, database_url: str):
    _reset(database_url)
    provider = FakeBatchProvider("cheap", status="in_progress")
    _patch_runtime(monkeypatch, database_url, tmp_path, {"cheap": provider, "openai": FakeProvider("openai")})
    runner = CliRunner()

    first = runner.invoke(app, ["radar", "reextract", "--no-wait"])
    assert first.exit_code == 0, first.output
    assert "batch in_progress; run `orbit radar reextract` again" in first.output

    provider.status = "ended"
    second = runner.invoke(app, ["radar", "reextract"])
    assert second.exit_code == 0, second.output
    assert "resuming batch msgbatch_1 (3 postings, extract-small)" in second.output
    assert "3 re-extracted" in second.output
    assert len(provider.submitted) == 1  # no second, paid batch


def test_cli_reextract_without_batch_support_fails_cleanly(monkeypatch, tmp_path: Path, database_url: str):
    _reset(database_url)
    _patch_runtime(monkeypatch, database_url, tmp_path, {"cheap": FakeProvider("cheap"), "openai": FakeProvider("x")})

    result = CliRunner().invoke(app, ["radar", "reextract"])

    assert result.exit_code == 1
    assert "does not support batches" in result.output
