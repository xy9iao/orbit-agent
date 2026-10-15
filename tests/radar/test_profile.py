"""Resume/goal profile (M5.2): hashing, skip-if-unchanged, upsert, and `orbit radar resume`."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from typer.testing import CliRunner

import orbit.radar.cli as radar_cli
from orbit.cli.main import app
from orbit.config import Settings
from orbit.core.router import ModelRouter
from orbit.radar.profile import (
    MAX_EMBED_CHARS,
    get_profile,
    load_resume,
    set_goal,
    set_profile,
    set_resume,
)
from orbit.storage.db import Database
from orbit.storage.models import EMBEDDING_DIM, Profile
from tests.conftest import FakeProvider, fake_embedding, make_config

RESUME = "# Xinyang\n\nWaterloo CS 3A. Python, PyTorch, Postgres. Built Orbit, an always-on agent.\n"


def _router() -> tuple[ModelRouter, FakeProvider]:
    fake = FakeProvider("cheap")
    return ModelRouter(make_config(), {"cheap": fake, "openai": FakeProvider("openai")}), fake


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


async def test_set_resume_stores_text_hash_and_embedding(db: Database, tmp_path: Path):
    router, fake = _router()
    result = await set_resume(db, router, _write(tmp_path, "resume.md", RESUME))

    stored = RESUME.strip()
    assert result.changed and result.kind == "resume" and not result.truncated
    assert result.chars == len(stored)
    assert result.text_hash == hashlib.sha256(stored.encode()).hexdigest()
    assert fake.embed_calls == [{"model": "embed-small", "texts": [stored], "params": {}}]

    row = await get_profile(db, "resume")
    assert row is not None
    assert row.text == stored and row.text_hash == result.text_hash and row.updated_at is not None
    assert len(row.embedding) == EMBEDDING_DIM
    assert list(row.embedding) == pytest.approx(fake_embedding(stored), abs=1e-6)


async def test_unchanged_text_is_not_re_embedded(db: Database, tmp_path: Path):
    router, fake = _router()
    path = _write(tmp_path, "resume.md", RESUME)
    await set_resume(db, router, path)
    # Same content with CRLF line endings and extra whitespace normalizes to the same text.
    crlf = tmp_path / "resume.txt"
    crlf.write_bytes(("\n  " + RESUME.replace("\n", "\r\n") + "  \n").encode("utf-8"))

    again = await set_resume(db, router, crlf)

    assert again.changed is False
    assert len(fake.embed_calls) == 1


async def test_changed_text_re_embeds_and_updates_row(db: Database, tmp_path: Path):
    router, fake = _router()
    path = _write(tmp_path, "resume.md", RESUME)
    first = await set_resume(db, router, path)
    path.write_text(RESUME + "\nNew: Kubernetes internship.\n", encoding="utf-8")

    second = await set_resume(db, router, path)

    assert second.changed and second.text_hash != first.text_hash
    assert len(fake.embed_calls) == 2
    row = await get_profile(db, "resume")
    assert row.text_hash == second.text_hash and "Kubernetes" in row.text
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Profile)) == 1


async def test_missing_embedding_is_re_embedded_even_if_hash_matches(db: Database):
    router, fake = _router()
    first = await set_goal(db, router, "ML infra co-op in Toronto")
    async with db.session() as s:
        await s.execute(text("UPDATE radar_profile SET embedding = NULL"))
        await s.commit()

    again = await set_goal(db, router, "ML infra co-op in Toronto")

    assert again.changed and again.text_hash == first.text_hash
    assert len(fake.embed_calls) == 2
    assert (await get_profile(db, "goal")).embedding is not None


async def test_goal_is_stored_independently_of_resume(db: Database, tmp_path: Path):
    router, _ = _router()
    await set_resume(db, router, _write(tmp_path, "resume.md", RESUME))
    goal = await set_goal(db, router, "  Backend or ML internship, Summer 2027, Toronto or remote  ")

    assert goal.kind == "goal" and goal.changed
    resume_row, goal_row = await get_profile(db, "resume"), await get_profile(db, "goal")
    assert resume_row.text == RESUME.strip()
    assert goal_row.text == "Backend or ML internship, Summer 2027, Toronto or remote"
    assert list(goal_row.embedding) != list(resume_row.embedding)


async def test_long_text_truncated_for_embedder_but_stored_in_full(db: Database):
    router, fake = _router()
    long_text = "x" * (MAX_EMBED_CHARS + 500)

    result = await set_profile(db, router, "resume", long_text)

    assert result.truncated
    assert len(fake.embed_calls[0]["texts"][0]) == MAX_EMBED_CHARS
    assert len((await get_profile(db, "resume")).text) == MAX_EMBED_CHARS + 500


async def test_empty_text_and_bad_kind_rejected(db: Database):
    router, fake = _router()
    with pytest.raises(ValueError, match="empty"):
        await set_goal(db, router, " \n\t ")
    with pytest.raises(ValueError, match="kind"):
        await set_profile(db, router, "cover_letter", "hi")
    assert fake.embed_calls == []
    assert await get_profile(db, "goal") is None


def test_load_resume_rejects_pdf_and_missing(tmp_path: Path):
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    with pytest.raises(ValueError, match=r"\.md or \.txt"):
        load_resume(pdf)
    with pytest.raises(FileNotFoundError):
        load_resume(tmp_path / "nope.md")
    bom = tmp_path / "bom.txt"
    bom.write_bytes("﻿hello".encode())
    assert load_resume(bom) == "hello"


# ---- CLI --------------------------------------------------------------------------------------
# CliRunner is synchronous and the command calls asyncio.run, so these tests are sync and build their own
# Database on the test URL inside the patched runtime instead of using the async `db` fixture.


def _patch_runtime(monkeypatch, database_url: str, tmp_path: Path, providers: dict) -> None:
    @asynccontextmanager
    async def fake_runtime(settings=None):
        database = Database(database_url)
        await database.connect()
        try:
            config = make_config()
            router = ModelRouter(config, providers, usage_sink=database.add_usage)
            yield radar_cli.RadarRuntime(Settings(orbit_home=tmp_path), config, database, router)
        finally:
            await database.close()

    monkeypatch.setattr(radar_cli, "open_runtime", fake_runtime)


async def _sql(database_url: str, sql: str) -> list:
    database = Database(database_url)
    await database.connect()
    try:
        async with database.engine.begin() as conn:
            result = await conn.execute(text(sql))
            return [tuple(r) for r in result.all()] if result.returns_rows else []
    finally:
        await database.close()


def test_cli_radar_resume(monkeypatch, tmp_path: Path, database_url: str):
    asyncio.run(_sql(database_url, "TRUNCATE radar_profile, usage RESTART IDENTITY"))
    fake = FakeProvider("cheap")
    _patch_runtime(monkeypatch, database_url, tmp_path, {"cheap": fake})
    path = _write(tmp_path, "resume.md", RESUME)
    runner = CliRunner()

    first = runner.invoke(app, ["radar", "resume", str(path)])
    second = runner.invoke(app, ["radar", "resume", str(path)])

    digest = hashlib.sha256(RESUME.strip().encode()).hexdigest()
    assert first.exit_code == 0, first.output
    assert f"resume embedded: {len(RESUME.strip())} chars, sha256 {digest[:12]}" in first.output
    assert second.exit_code == 0, second.output
    assert "unchanged, not re-embedded" in second.output
    assert len(fake.embed_calls) == 1
    assert asyncio.run(_sql(database_url, "SELECT kind, text_hash FROM radar_profile")) == [("resume", digest)]
    assert asyncio.run(_sql(database_url, "SELECT task, model FROM usage")) == [("embed", "embed-small")]


def test_cli_radar_resume_errors(monkeypatch, tmp_path: Path, database_url: str):
    _patch_runtime(monkeypatch, database_url, tmp_path, {})  # no provider: as if OPENAI_API_KEY were unset
    runner = CliRunner()

    missing = runner.invoke(app, ["radar", "resume", str(tmp_path / "missing.md")])
    pdf = runner.invoke(app, ["radar", "resume", str(_write(tmp_path, "cv.pdf", "x"))])
    no_key = runner.invoke(app, ["radar", "resume", str(_write(tmp_path, "resume.md", RESUME))])

    assert missing.exit_code == 1 and "not found" in missing.output
    assert pdf.exit_code == 1 and "PDF is not supported" in pdf.output
    assert no_key.exit_code == 1 and "OPENAI_API_KEY" in no_key.output
