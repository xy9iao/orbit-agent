"""Posting embeddings (M5.3) and scoring (M5.4): text format, batching, hash guard, skip rules, failures,
rule filters, cosine scores, pipeline and CLI.

FakeProvider's embed returns deterministic 1536-d unit vectors seeded by the text (conftest.fake_embedding).
Scoring tests use crafted vectors (`vec(0.8, 0.6)`) whose cosine similarities are known exactly.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, update
from typer.testing import CliRunner

from orbit.cli.main import app
from orbit.config import RadarConfig, TaskConfig, load_config
from orbit.core.providers.base import EmbedResponse
from orbit.core.router import ModelRouter
from orbit.radar import match
from orbit.radar.extract import TRUNCATION_MARKER
from orbit.radar.match import (
    MAX_EMBED_DESCRIPTION_CHARS,
    EmbedSummary,
    ScoreSummary,
    embed_pending,
    embedding_text,
    score_postings,
    wanted_locations,
    wanted_terms,
)
from orbit.radar.pipeline import describe, run_pipeline
from orbit.storage.db import Database
from orbit.storage.models import EMBEDDING_DIM, Company, Posting, Profile
from tests.conftest import FakeProvider, fake_embedding, make_config
from tests.radar.test_extract import GOOD, GOOD_NORMALIZED, make_router, reply
from tests.radar.test_pipeline import _patch_runtime
from tests.radar.test_poll import FakeBoards, acme_payload, make_client, no_network  # noqa: F401
from tests.radar.test_seed import sql

NOW = datetime(2026, 9, 2, 12, 0, 0)
NOT_INTERNSHIP = {**GOOD_NORMALIZED, "is_internship": False}
SKIPPED = {"is_internship": False, "skipped_reason": "title: Senior Engineer (matched 'Senior')"}


def text_of(**kw: Any) -> str:
    fields = {
        "title": "ML Intern",
        "description_text": "Train models.\n\nMay to August 2027.",
        "extraction_status": "ok",
        "extraction": GOOD_NORMALIZED,
    }
    fields.update(kw)
    return embedding_text(SimpleNamespace(**fields), "Acme AI")


# ---- embedding_text ---------------------------------------------------------------------------------


def test_embedding_text_format_with_extraction():
    assert text_of() == (
        "Title: ML Intern\n"
        "Company: Acme AI\n"
        "Terms: Summer 2027, Fall 2027\n"
        "Locations: Toronto, ON\n"
        "Role type: ml_ai\n"
        "Tech stack: python, pytorch, kubernetes\n"
        "\n"
        "Train models.\n\nMay to August 2027."
    )


def test_embedding_text_omits_empty_and_unextracted_fields():
    sparse = {**GOOD_NORMALIZED, "terms": [], "tech_stack": [], "locations": ["Toronto, ON", "Remote (Canada)"]}
    assert text_of(extraction=sparse) == (
        "Title: ML Intern\nCompany: Acme AI\nLocations: Toronto, ON; Remote (Canada)\nRole type: ml_ai\n\n"
        "Train models.\n\nMay to August 2027."
    )
    # Extracted fields only count when extraction succeeded; skipped/pending rows get title + company + text.
    base = "Title: ML Intern\nCompany: Acme AI\n\nTrain models.\n\nMay to August 2027."
    assert text_of(extraction_status="skipped", extraction=SKIPPED) == base
    assert text_of(extraction_status="pending", extraction=None) == base
    assert text_of(description_text="  ") == "\n".join(text_of().splitlines()[:6])


def test_embedding_text_truncates_the_description():
    out = text_of(description_text="word " * 5000)  # 25k chars
    description = out.split("\n\n", 1)[1]
    assert description.endswith(TRUNCATION_MARKER)
    assert len(description) <= MAX_EMBED_DESCRIPTION_CHARS + len(TRUNCATION_MARKER)
    assert len(description) > MAX_EMBED_DESCRIPTION_CHARS // 2


# ---- embed_pending (real Postgres) ------------------------------------------------------------------


async def _company(db: Database) -> Company:
    async with db.session() as s:
        company = Company(slug="acme", name="Acme AI", source="greenhouse", board_token="acme")
        s.add(company)
        await s.commit()
        return company


def _row(company: Company, external_id: str, *, minutes: int = 0, **kw: Any) -> Posting:
    fields: dict[str, Any] = {
        "source": "greenhouse",
        "external_id": external_id,
        "company_id": company.id,
        "title": f"Software Engineering Intern {external_id}",
        "url": f"https://example.com/{external_id}",
        "location_raw": "Toronto, ON",
        "department": None,
        "description_text": f"Posting {external_id}: build things.",
        "raw": {},
        "content_hash": f"hash-{external_id}",
        "first_seen_at": NOW + timedelta(minutes=minutes),
        "last_seen_at": NOW,
        "extraction_status": "ok",
        "extraction": GOOD_NORMALIZED,
    }
    fields.update(kw)
    return Posting(**fields)


async def _add(db: Database, *rows: Posting) -> None:
    async with db.session() as s:
        s.add_all(rows)
        await s.commit()


async def _rows(db: Database) -> dict[str, Posting]:
    async with db.session() as s:
        return {p.external_id: p for p in (await s.execute(select(Posting))).scalars()}


def _router(provider: FakeProvider) -> ModelRouter:
    return ModelRouter(make_config(), {"cheap": provider, "openai": FakeProvider("openai")})


def _vector(row: Posting) -> list[float]:
    return [float(x) for x in row.embedding]


async def test_embeds_in_batches_of_64(db: Database):
    company = await _company(db)
    await _add(db, *(_row(company, f"{i:03d}", minutes=i) for i in range(130)))
    fake = FakeProvider("cheap")

    summary = await embed_pending(db, _router(fake))

    assert summary == EmbedSummary(embedded=130, batches=3)
    assert [len(c["texts"]) for c in fake.embed_calls] == [64, 64, 2]
    assert {c["model"] for c in fake.embed_calls} == {"embed-small"}
    rows = await _rows(db)
    assert all(p.embedded_hash == p.content_hash for p in rows.values())
    newest = rows["129"]  # newest first
    assert fake.embed_calls[0]["texts"][0] == embedding_text(newest, "Acme AI")
    assert _vector(newest) == pytest.approx(fake_embedding(embedding_text(newest, "Acme AI")), abs=1e-6)

    again = await embed_pending(db, _router(fake))
    assert again == EmbedSummary() and len(fake.embed_calls) == 3  # nothing left: no call


async def test_batch_size_and_limit(db: Database):
    company = await _company(db)
    await _add(db, *(_row(company, str(i), minutes=i) for i in range(5)))
    fake = FakeProvider("cheap")

    summary = await embed_pending(db, _router(fake), batch_size=2, limit=3)

    assert summary == EmbedSummary(embedded=3, batches=2)
    assert [len(c["texts"]) for c in fake.embed_calls] == [2, 1]
    embedded = sorted(k for k, p in (await _rows(db)).items() if p.embedded_hash)
    assert embedded == ["2", "3", "4"]
    assert await embed_pending(db, _router(fake), limit=0) == EmbedSummary()
    await embed_pending(db, _router(fake), batch_size=1000)
    assert len(fake.embed_calls[-1]["texts"]) == 2  # the rest; batch_size above 64 is capped


async def test_skip_rules(db: Database):
    company = await _company(db)
    await _add(
        db,
        _row(company, "eligible"),
        _row(company, "changed", embedded_hash="hash-old", embedding=fake_embedding("old")),
        _row(company, "current", embedded_hash="hash-current", embedding=fake_embedding("current")),
        _row(company, "closed", status="closed"),
        _row(company, "pending", extraction_status="pending", extraction=None),
        _row(company, "failed", extraction_status="failed", extraction=None, extraction_error="boom"),
        _row(company, "prefiltered", extraction_status="skipped", extraction=SKIPPED),
        _row(company, "fulltime", extraction=NOT_INTERNSHIP),
    )
    fake = FakeProvider("cheap")

    summary = await embed_pending(db, _router(fake))

    assert summary == EmbedSummary(embedded=2, batches=1)
    rows = await _rows(db)
    assert {k for k, p in rows.items() if p.embedded_hash == p.content_hash} == {"eligible", "changed", "current"}
    changed_text = embedding_text(rows["changed"], "Acme AI")
    assert _vector(rows["changed"]) == pytest.approx(fake_embedding(changed_text), abs=1e-6)
    assert _vector(rows["current"]) == pytest.approx(fake_embedding("current"), abs=1e-6)  # untouched
    for key in ("closed", "pending", "failed", "prefiltered", "fulltime"):
        assert rows[key].embedding is None and rows[key].embedded_hash is None


async def test_hash_guard_discards_vectors_for_rows_that_changed(db: Database):
    company = await _company(db)
    await _add(db, _row(company, "a", minutes=1), _row(company, "b"))

    class ChangingProvider(FakeProvider):
        async def embed(self, model, texts, **params):
            # A poll lands while the embed call is in flight: new content for "a".
            async with db.session() as s:
                await s.execute(update(Posting).where(Posting.external_id == "a").values(content_hash="hash-new"))
                await s.commit()
            return await super().embed(model, texts, **params)

    summary = await embed_pending(db, _router(ChangingProvider("cheap")))

    assert summary == EmbedSummary(embedded=1, stale=1, batches=1)
    rows = await _rows(db)
    assert rows["a"].embedding is None and rows["a"].embedded_hash is None  # re-embedded next run
    assert rows["b"].embedded_hash == "hash-b"


class FlakyProvider(FakeProvider):
    """Embed calls listed in `fail_calls` (1-based) raise; `short` returns one vector too few instead."""

    def __init__(self, *args: Any, fail_calls: set[int] = frozenset(), short: bool = False, **kw: Any):
        super().__init__(*args, **kw)
        self.fail_calls = set(fail_calls)
        self.short = short

    async def embed(self, model, texts, **params):
        response = await super().embed(model, texts, **params)
        if len(self.embed_calls) in self.fail_calls:
            if self.short:
                return EmbedResponse(vectors=response.vectors[:-1], usage=response.usage)
            raise ConnectionError("connection reset by peer")
        return response


async def test_failed_batch_leaves_rows_pending_and_others_continue(db: Database):
    company = await _company(db)
    await _add(db, *(_row(company, str(i), minutes=i) for i in range(4)))
    fake = FlakyProvider("cheap", fail_calls={1})

    summary = await embed_pending(db, _router(fake), batch_size=2)

    assert summary == EmbedSummary(embedded=2, failed=2, batches=2)
    rows = await _rows(db)
    assert [rows[str(i)].embedded_hash is None for i in range(4)] == [False, False, True, True]  # newest failed

    retry = await embed_pending(db, _router(fake), batch_size=2)
    assert retry == EmbedSummary(embedded=2, batches=1)


async def test_vector_count_mismatch_fails_the_batch(db: Database):
    company = await _company(db)
    await _add(db, _row(company, "a"), _row(company, "b"))

    summary = await embed_pending(db, _router(FlakyProvider("cheap", fail_calls={1}, short=True)))

    assert summary == EmbedSummary(failed=2, batches=1)
    assert all(p.embedded_hash is None for p in (await _rows(db)).values())


async def test_wrong_dimension_fails_the_batch(db: Database):
    company = await _company(db)
    await _add(db, _row(company, "a"))

    summary = await embed_pending(db, _router(FakeProvider("cheap", embed_dim=3)))

    assert summary == EmbedSummary(failed=1, batches=1)
    assert (await _rows(db))["a"].embedded_hash is None


async def test_stops_after_repeated_batch_failures(db: Database):
    company = await _company(db)
    await _add(db, *(_row(company, str(i), minutes=i) for i in range(5)))
    fake = FlakyProvider("cheap", fail_calls={1, 2, 3, 4, 5})

    summary = await embed_pending(db, _router(fake), batch_size=1)

    n = match.MAX_CONSECUTIVE_BATCH_ERRORS
    assert summary == EmbedSummary(failed=n, batches=n, aborted=True)
    assert len(fake.embed_calls) == n


async def test_unresolvable_embed_task_raises_before_reading_rows(db: Database):
    company = await _company(db)
    await _add(db, _row(company, "a"))

    with pytest.raises(RuntimeError, match="not configured"):
        await embed_pending(db, ModelRouter(make_config(), {"openai": FakeProvider("openai")}))


# ---- pipeline ---------------------------------------------------------------------------------------


async def _acme(db: Database) -> Company:
    async with db.session() as s:
        company = Company(slug="acme", name="Acme", source="greenhouse", board_token="acme", active=True)
        s.add(company)
        await s.commit()
        return company


async def _run(db: Database, router: ModelRouter, boards: FakeBoards):
    async with make_client(boards) as client:
        return await run_pipeline(db, router, client, RadarConfig(), now=NOW)


async def test_pipeline_embeds_after_extraction(db: Database):
    await _acme(db)
    fake = FakeProvider("cheap", [reply(GOOD)])
    boards = FakeBoards(acme=acme_payload())

    summary = await _run(db, make_router(fake), boards)

    assert summary.ok and summary.extract.ok == 1
    assert summary.embed == EmbedSummary(embedded=1, batches=1)
    [row] = (await _rows(db)).values()
    text = embedding_text(row, "Acme")
    assert fake.embed_calls[0]["texts"] == [text]
    assert "Terms: Summer 2027, Fall 2027" in text and "Title: Software Developer Co-op (Winter 2027)" in text
    assert row.embedded_hash == row.content_hash
    assert _vector(row) == pytest.approx(fake_embedding(text), abs=1e-6)

    second = await _run(db, make_router(fake), boards)

    assert second.embed == EmbedSummary() and len(fake.embed_calls) == 1
    assert "embed: embedded 0, failed 0, stale 0 in 0 batches" in describe(second)


async def test_pipeline_skips_embedding_without_provider_and_still_extracts(db: Database):
    await _acme(db)
    config = make_config()
    config.tasks["embed"] = TaskConfig(provider="openai", model="text-embedding-3-small")
    fake = FakeProvider("cheap", [reply(GOOD)])
    router = ModelRouter(config, {"cheap": fake})  # no "openai" provider: embed has no key

    summary = await _run(db, router, FakeBoards(acme=acme_payload()))

    assert summary.ok and summary.extract.ok == 1 and summary.embed is None
    assert summary.skipped == {"embed": "no provider for task 'embed' — set OPENAI_API_KEY"}
    assert fake.embed_calls == []


async def test_pipeline_embed_failure_is_isolated(db: Database):
    await _acme(db)
    fake = FlakyProvider("cheap", [reply(GOOD)], fail_calls={1})

    summary = await _run(db, make_router(fake), FakeBoards(acme=acme_payload()))

    assert summary.ok and summary.poll.new == 1 and summary.extract.ok == 1
    assert summary.embed == EmbedSummary(failed=1, batches=1)
    [row] = (await _rows(db)).values()
    assert row.extraction_status == "ok" and row.embedded_hash is None  # retried next run


# ---- `orbit radar embed` ----------------------------------------------------------------------------


def _seed(database_url: str, count: int) -> None:
    async def _go() -> None:
        await sql(database_url, "TRUNCATE radar_companies RESTART IDENTITY CASCADE")
        database = Database(database_url)
        await database.connect()
        try:
            company = await _company(database)
            await _add(database, *(_row(company, str(i), minutes=i) for i in range(count)))
        finally:
            await database.close()

    asyncio.run(_go())


def _embedded(database_url: str) -> int:
    query = "SELECT count(*) FROM radar_postings WHERE embedded_hash = content_hash"
    return asyncio.run(sql(database_url, query))[0][0]


def test_cli_embed(monkeypatch, tmp_path: Path, database_url: str):
    _seed(database_url, 3)
    fake = FakeProvider("cheap")
    _patch_runtime(monkeypatch, database_url, tmp_path, {"cheap": fake})
    runner = CliRunner()

    first = runner.invoke(app, ["radar", "embed", "--limit", "2"])

    assert first.exit_code == 0, first.output
    assert "embed: embedded 2, failed 0, stale 0 in 1 batches" in first.output
    assert _embedded(database_url) == 2

    second = runner.invoke(app, ["radar", "embed"])

    assert second.exit_code == 0, second.output
    assert "embed: embedded 1," in second.output and _embedded(database_url) == 3


def test_cli_embed_without_provider_fails_cleanly(monkeypatch, tmp_path: Path, database_url: str):
    _seed(database_url, 1)
    _patch_runtime(monkeypatch, database_url, tmp_path, {})

    result = CliRunner().invoke(app, ["radar", "embed"])

    assert result.exit_code == 1 and "OPENAI_API_KEY" in result.output
    assert _embedded(database_url) == 0


def test_cli_embed_exit_1_after_repeated_failures(monkeypatch, tmp_path: Path, database_url: str):
    _patch_runtime(monkeypatch, database_url, tmp_path, {"cheap": FlakyProvider("cheap", fail_calls={1})})
    monkeypatch.setattr(match, "MAX_CONSECUTIVE_BATCH_ERRORS", 1)
    monkeypatch.setattr(match, "MAX_BATCH_SIZE", 1)  # caps the CLI's default batch size: two batches
    _seed(database_url, 2)

    result = CliRunner().invoke(app, ["radar", "embed"])

    assert result.exit_code == 1
    assert "failed 1" in result.output and "stopped after repeated embedding failures" in result.output


# ---- scoring (M5.4) ---------------------------------------------------------------------------------


def vec(*leading: float) -> list[float]:
    """A 1536-d vector with the given leading components (unit length when they are)."""
    return [*leading, *([0.0] * (EMBEDDING_DIM - len(leading)))]


async def _profile(db: Database, kind: str, vector: list[float] | None) -> None:
    async with db.session() as s:
        s.add(Profile(kind=kind, text=f"{kind} text", text_hash=f"{kind}-hash", embedding=vector))
        await s.commit()


async def _set_profile(db: Database, kind: str, vector: list[float]) -> None:
    async with db.session() as s:
        await s.execute(update(Profile).where(Profile.kind == kind).values(embedding=vector))
        await s.commit()


def _scorable(company: Company, external_id: str, vector: list[float], **kw: Any) -> Posting:
    """An eligible posting (Summer/Fall 2027, Toronto, internship) with a current embedding."""
    kw.setdefault("embedded_hash", f"hash-{external_id}")
    return _row(company, external_id, embedding=vector, **kw)


def _with(**fields: Any) -> dict[str, Any]:
    return {**GOOD_NORMALIZED, **fields}


async def _scores(db: Database) -> dict[str, float | None]:
    return {k: p.score for k, p in (await _rows(db)).items()}


async def test_scores_are_cosine_similarity_to_the_resume(db: Database):
    company = await _company(db)
    await _profile(db, "resume", vec(1.0))
    await _add(
        db,
        _scorable(company, "close", vec(0.9, 0.43588989)),  # cos = 0.9
        _scorable(company, "middle", vec(0.5, 0.8660254)),  # cos = 0.5
        _scorable(company, "orthogonal", vec(0.0, 1.0)),  # cos = 0
        _scorable(company, "opposite", vec(-0.6, 0.8)),  # cos = -0.6
        _scorable(company, "unnormalized", vec(3.0, 4.0)),  # cosine ignores length: 0.6
    )

    summary = await score_postings(db, RadarConfig(), now=NOW)

    assert summary == ScoreSummary(scored=5)
    rows = await _rows(db)
    expected = {"close": 0.9, "middle": 0.5, "orthogonal": 0.0, "opposite": -0.6, "unnormalized": 0.6}
    assert {k: p.score for k, p in rows.items()} == pytest.approx(expected, abs=1e-6)
    assert {p.scored_at for p in rows.values()} == {NOW}
    ranked = sorted(rows, key=lambda k: rows[k].score, reverse=True)
    assert ranked == ["close", "unnormalized", "middle", "orthogonal", "opposite"]


async def test_goal_is_blended_80_20(db: Database):
    company = await _company(db)
    await _profile(db, "resume", vec(1.0))
    await _profile(db, "goal", vec(0.0, 1.0))
    await _add(db, _scorable(company, "a", vec(0.8, 0.6)), _scorable(company, "b", vec(0.6, 0.8)))

    summary = await score_postings(db, RadarConfig(), now=NOW)

    assert summary == ScoreSummary(scored=2, goal=True)
    # a: 0.8 * 0.8 + 0.2 * 0.6 = 0.76;  b: 0.8 * 0.6 + 0.2 * 0.8 = 0.64
    assert await _scores(db) == pytest.approx({"a": 0.76, "b": 0.64}, abs=1e-6)
    assert match.describe_score(summary) == "scored 2, cleared 0 (resume + goal)"


async def test_goal_without_embedding_is_ignored(db: Database):
    company = await _company(db)
    await _profile(db, "resume", vec(1.0))
    await _profile(db, "goal", None)
    await _add(db, _scorable(company, "a", vec(0.8, 0.6)))

    assert await score_postings(db, RadarConfig(), now=NOW) == ScoreSummary(scored=1)
    assert await _scores(db) == pytest.approx({"a": 0.8}, abs=1e-6)


async def test_rule_filters(db: Database):
    company = await _company(db)
    await _profile(db, "resume", vec(1.0))
    v = vec(0.8, 0.6)
    remote_canada = _with(locations=["San Francisco, CA"], remote_ok=True, canada_eligible=True)
    remote_us = _with(locations=["Remote (US)"], remote_ok=True, canada_eligible=False)
    await _add(
        db,
        _scorable(company, "eligible", v),
        _scorable(company, "not_intern", v, extraction=NOT_INTERNSHIP),
        _scorable(company, "wrong_term", v, extraction=_with(terms=["Winter 2027"])),
        _scorable(company, "one_term_overlaps", v, extraction=_with(terms=["Winter 2027", "Fall 2027"])),
        _scorable(company, "no_terms", v, extraction=_with(terms=[])),
        _scorable(company, "waterloo", v, extraction=_with(locations=["Vancouver, BC", "Waterloo, ON"])),
        _scorable(company, "wrong_city", v, extraction=_with(locations=["Vancouver, BC"])),
        _scorable(company, "remote_canada", v, extraction=remote_canada),
        _scorable(company, "remote_listed", v, extraction=_with(locations=["Remote (Canada)"], remote_ok=False)),
        _scorable(company, "remote_us_only", v, extraction=remote_us),
        _scorable(company, "no_location", v, extraction=_with(locations=[], remote_ok=False)),
        _scorable(company, "no_location_remote", v, extraction=_with(locations=[], remote_ok=True)),
        _scorable(company, "closed", v, status="closed", score=0.3, scored_at=NOW - timedelta(days=1)),
        _scorable(company, "failed", v, extraction_status="failed", extraction=None, score=0.4),
        _scorable(company, "prefiltered", v, extraction_status="skipped", extraction=SKIPPED, score=0.4),
        _scorable(company, "stale_embedding", v, embedded_hash="hash-old", score=0.4),
        _row(company, "not_embedded"),
    )

    summary = await score_postings(db, RadarConfig(), now=NOW)

    scores = await _scores(db)
    passing = {
        "eligible", "one_term_overlaps", "no_terms", "waterloo", "remote_canada", "remote_listed",
        "no_location_remote",
    }
    assert {k for k, score in scores.items() if score is not None} == passing | {"closed"}
    assert all(scores[k] == pytest.approx(0.8, abs=1e-6) for k in passing)
    assert scores["closed"] == 0.3  # closed postings keep their last score (history only)
    assert summary == ScoreSummary(scored=len(passing), cleared=3)  # failed, prefiltered, stale_embedding
    rows = await _rows(db)
    assert rows["failed"].scored_at is None and rows["closed"].scored_at == NOW - timedelta(days=1)


async def test_posting_that_stops_passing_the_filters_loses_its_score(db: Database):
    company = await _company(db)
    await _profile(db, "resume", vec(1.0))
    await _add(db, _scorable(company, "a", vec(0.8, 0.6)), _scorable(company, "b", vec(0.6, 0.8)))
    assert (await score_postings(db, RadarConfig(), now=NOW)).scored == 2

    # The user narrows radar.terms: neither posting matches any more.
    narrowed = RadarConfig(terms=["Winter 2028"])
    assert await score_postings(db, narrowed, now=NOW) == ScoreSummary(scored=0, cleared=2)
    assert await _scores(db) == {"a": None, "b": None}
    assert (await score_postings(db, narrowed, now=NOW)).cleared == 0  # already NULL: nothing to clear


async def test_every_run_rescores_so_a_new_resume_takes_effect(db: Database):
    company = await _company(db)
    await _profile(db, "resume", vec(1.0))
    await _add(db, _scorable(company, "a", vec(0.8, 0.6)))
    await score_postings(db, RadarConfig(), now=NOW)

    await _set_profile(db, "resume", vec(0.0, 1.0))
    later = NOW + timedelta(minutes=30)
    assert await score_postings(db, RadarConfig(), now=later) == ScoreSummary(scored=1)

    [row] = (await _rows(db)).values()
    assert row.score == pytest.approx(0.6, abs=1e-6) and row.scored_at == later


async def test_config_values_are_normalized_and_empty_lists_disable_a_filter(db: Database):
    company = await _company(db)
    await _profile(db, "resume", vec(1.0))
    v = vec(1.0)
    vancouver_winter = _with(terms=["Winter 2027"], locations=["Vancouver, BC"])
    await _add(
        db,
        _scorable(company, "toronto_summer", v, extraction=_with(terms=["Summer 2027"])),
        _scorable(company, "vancouver_winter", v, extraction=vancouver_winter),
    )

    loose = RadarConfig(terms=["summer '27"], locations=["toronto"])
    assert wanted_terms(loose.terms) == ["Summer 2027"] and wanted_locations(loose.locations) == ["Toronto, ON"]
    assert (await score_postings(db, loose, now=NOW)).scored == 1
    assert (await score_postings(db, RadarConfig(terms=[], locations=[]), now=NOW)).scored == 2
    # An entry that can't be parsed is kept as written: it matches nothing but doesn't break scoring.
    assert wanted_terms(["Summer 2027", "whenever"]) == ["Summer 2027", "whenever"]
    assert wanted_terms(["May-Dec 2027"]) == ["Summer 2027", "Fall 2027"]


async def test_role_types_is_a_hard_filter(db: Database):
    company = await _company(db)
    await _profile(db, "resume", vec(1.0))
    close = vec(1.0)  # similarity 1.0: only the role filter can keep these out
    await _add(
        db,
        _scorable(company, "ml", close, extraction=_with(role_type="ml_ai")),
        _scorable(company, "swe", close, extraction=_with(role_type="swe")),
        _scorable(company, "finance", close, extraction=_with(role_type="other")),
        _scorable(company, "product", close, extraction=_with(role_type="product")),
        _scorable(company, "no_role", close, extraction={k: v for k, v in GOOD_NORMALIZED.items() if k != "role_type"}),
    )

    technical = RadarConfig(role_types=["swe", "ml_ai", "infra", "data", "research"])
    summary = await score_postings(db, technical, now=NOW)

    scores = await _scores(db)
    assert {k for k, s in scores.items() if s is not None} == {"ml", "swe"}
    assert summary == ScoreSummary(scored=2)

    # Narrowing the list later clears scores that no longer qualify, so they can never notify.
    assert await score_postings(db, RadarConfig(role_types=["swe"]), now=NOW) == ScoreSummary(scored=1, cleared=1)
    # An empty list means any role.
    assert (await score_postings(db, RadarConfig(role_types=[]), now=NOW)).scored == 5


def test_role_types_config_is_validated_and_normalized():
    cfg = RadarConfig(role_types=["SWE", " ml_ai", "swe", "Research"])
    assert cfg.role_types == ["swe", "ml_ai", "research"]
    assert RadarConfig().role_types == []
    with pytest.raises(ValueError, match="unknown radar.role_types entry 'finance'"):
        RadarConfig(role_types=["swe", "finance"])


def test_repo_config_role_types_parse():
    assert load_config().radar.role_types == ["swe", "ml_ai", "infra", "data", "research"]


async def test_no_resume_is_a_no_op(db: Database):
    company = await _company(db)
    await _profile(db, "goal", vec(1.0))  # a goal alone is not enough
    await _add(
        db,
        _scorable(company, "a", vec(1.0)),
        _scorable(company, "b", vec(1.0), extraction=NOT_INTERNSHIP, score=0.5),
    )

    summary = await score_postings(db, RadarConfig(), now=NOW)

    assert summary == ScoreSummary(no_resume=True)
    assert await _scores(db) == {"a": None, "b": 0.5}  # untouched
    assert match.describe_score(summary) == "no resume embedded yet"


@pytest.fixture
def warning_log():
    from loguru import logger

    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    yield messages
    logger.remove(sink)


async def test_pipeline_scores_after_embedding(db: Database, warning_log):
    await _acme(db)
    resume = fake_embedding("my resume")
    await _profile(db, "resume", resume)
    fake = FakeProvider("cheap", [reply(GOOD)])

    summary = await _run(db, make_router(fake), FakeBoards(acme=acme_payload()))

    assert summary.ok and summary.embed == EmbedSummary(embedded=1, batches=1)
    assert summary.score == ScoreSummary(scored=1)
    [row] = (await _rows(db)).values()
    expected = sum(a * b for a, b in zip(_vector(row), resume, strict=True))  # both unit vectors
    assert row.score == pytest.approx(expected, abs=1e-5) and row.scored_at == NOW
    assert "score: scored 1, cleared 0" in describe(summary)
    assert not [w for w in warning_log if "scoring skipped" in w]


async def test_pipeline_without_resume_warns_once_and_scores_nothing(db: Database, warning_log):
    await _acme(db)
    fake = FakeProvider("cheap", [reply(GOOD)])

    summary = await _run(db, make_router(fake), FakeBoards(acme=acme_payload()))

    assert summary.ok and summary.score == ScoreSummary(no_resume=True)
    assert [w for w in warning_log if "scoring skipped" in w] == [
        "radar pipeline: scoring skipped: no resume embedded yet — run `orbit radar resume`"
    ]
    assert "score: no resume embedded yet" in describe(summary)
    [row] = (await _rows(db)).values()
    assert row.score is None


async def test_pipeline_score_failure_is_isolated(db: Database, monkeypatch):
    await _acme(db)

    async def boom(*args: Any, **kw: Any) -> ScoreSummary:
        raise RuntimeError("pgvector exploded")

    monkeypatch.setattr(match, "score_postings", boom)
    fake = FakeProvider("cheap", [reply(GOOD)])

    summary = await _run(db, make_router(fake), FakeBoards(acme=acme_payload()))

    assert summary.errors == {"score": "RuntimeError: pgvector exploded"}
    assert summary.embed == EmbedSummary(embedded=1, batches=1) and summary.score is None
