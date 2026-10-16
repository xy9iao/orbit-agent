"""Seed companies (M2.5): seeds.yaml validation, upsert-by-slug semantics, and `orbit radar seed`."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, select, text
from typer.testing import CliRunner

import orbit.radar.cli as radar_cli
from orbit.cli.main import app
from orbit.config import Settings
from orbit.core.router import ModelRouter
from orbit.radar.seed import DEFAULT_SEEDS_PATH, SeedCompany, load_seeds, parse_seeds, seed_companies
from orbit.radar.sources import available_sources, get_adapter
from orbit.storage.db import Database
from orbit.storage.models import Company
from tests.conftest import make_config


def _seed(slug: str, **kw) -> SeedCompany:
    values = {"slug": slug, "name": slug.title(), "source": "greenhouse", "board_token": slug, "hq_city": "Toronto"}
    return SeedCompany(**(values | kw))


async def _companies(db: Database) -> dict[str, Company]:
    async with db.session() as s:
        return {c.slug: c for c in (await s.scalars(select(Company))).all()}


async def _run_seed(db: Database, seeds: list[SeedCompany], **kw):
    async with db.session() as s:
        result = await seed_companies(s, seeds, **kw)
        await s.commit()
    return result


# ---- the shipped seeds.yaml ---------------------------------------------------------------------


def test_real_seeds_file_is_valid():
    seeds = load_seeds()

    assert DEFAULT_SEEDS_PATH.name == "seeds.yaml" and DEFAULT_SEEDS_PATH.parent.name == "radar"
    assert len(seeds) >= 30
    slugs = [s.slug for s in seeds]
    assert len(set(slugs)) == len(slugs)
    assert {s.source for s in seeds} == set(available_sources()) == {"greenhouse", "lever", "ashby"}
    for s in seeds:
        assert s.name and s.board_token and s.hq_city
        url, _ = get_adapter(s.source).board_request(s.board_token)
        assert url.startswith("https://")


# ---- validation ---------------------------------------------------------------------------------


def _entry(slug: str, **kw) -> dict:
    return {"slug": slug, "name": slug, "source": "ashby", "board_token": slug} | kw


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"companies": [_entry("a", source="workday")]}, "unknown source 'workday'"),
        ({"companies": [_entry("a"), _entry("a", board_token="b")]}, "duplicate slug 'a'"),
        ({"companies": [_entry("a", board_token="X"), _entry("b", board_token="x")]}, "both use ashby board"),
        ({"companies": [_entry("Bad Slug")]}, "companies.0.slug"),
        ({"companies": [_entry("a", board_token=" ")]}, "companies.0.board_token"),
        ({"companies": [_entry("a", hq="Toronto")]}, "companies.0.hq"),
        ({"companies": []}, "companies"),
        (None, "(file)"),
    ],
)
def test_parse_seeds_rejects_bad_data(data, message):
    with pytest.raises(ValueError, match="invalid seed file") as info:
        parse_seeds(data)
    assert message in str(info.value)


def test_same_token_on_different_sources_is_allowed():
    seeds = parse_seeds({"companies": [_entry("a"), _entry("b", source="lever", board_token="a")]})
    assert [(s.slug, s.source, s.board_token) for s in seeds] == [("a", "ashby", "a"), ("b", "lever", "a")]


def test_load_seeds_errors(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_seeds(tmp_path / "missing.yaml")
    broken = tmp_path / "broken.yaml"
    broken.write_text("companies: [unclosed", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid YAML"):
        load_seeds(broken)


# ---- upsert semantics ---------------------------------------------------------------------------


async def test_seed_real_file_then_reseed_is_idempotent(db: Database):
    seeds = load_seeds()

    first = await _run_seed(db, seeds)
    before = await _companies(db)
    second = await _run_seed(db, seeds)
    after = await _companies(db)

    assert sorted(first.inserted) == sorted(s.slug for s in seeds)
    assert first.updated == first.unchanged == first.missing == []
    assert second.inserted == second.updated == [] and sorted(second.unchanged) == sorted(first.inserted)
    assert {slug: c.id for slug, c in after.items()} == {slug: c.id for slug, c in before.items()}
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Company)) == len(seeds)
    assert all(c.active for c in after.values())
    cohere = after["cohere"]
    assert (cohere.name, cohere.source, cohere.board_token, cohere.hq_city) == ("Cohere", "ashby", "cohere", "Toronto")


async def test_update_changes_fields_and_clears_stale_verification(db: Database):
    await _run_seed(db, [_seed("acme"), _seed("beta")])
    async with db.session() as s:
        await s.execute(
            text("UPDATE radar_companies SET last_verified_at = now(), last_verify_status = 'error', "
                 "last_verify_error = 'HTTP 404'")
        )
        await s.commit()

    result = await _run_seed(
        db, [_seed("acme", board_token="acme-inc", source="lever"), _seed("beta", name="Beta Corp", hq_city=None)]
    )

    rows = await _companies(db)
    assert sorted(result.updated) == ["acme", "beta"] and result.inserted == []
    assert (rows["acme"].source, rows["acme"].board_token) == ("lever", "acme-inc")
    # Board moved: the old verification result no longer describes this board.
    assert rows["acme"].last_verified_at is None and rows["acme"].last_verify_status is None
    assert rows["acme"].last_verify_error is None
    # Name/city change only: the board is the same, so its verification still holds.
    assert (rows["beta"].name, rows["beta"].hq_city) == ("Beta Corp", None)
    assert rows["beta"].last_verify_status == "error" and isinstance(rows["beta"].last_verified_at, datetime)


async def test_reseed_keeps_db_deactivation_unless_file_says_otherwise(db: Database):
    await _run_seed(db, [_seed("acme"), _seed("beta")])
    async with db.session() as s:
        await s.execute(text("UPDATE radar_companies SET active = false"))
        await s.commit()

    kept = await _run_seed(db, [_seed("acme"), _seed("beta")])
    assert sorted(kept.unchanged) == ["acme", "beta"]
    assert not any(c.active for c in (await _companies(db)).values())

    explicit = await _run_seed(db, [_seed("acme", active=True), _seed("beta")])
    rows = await _companies(db)
    assert explicit.updated == ["acme"] and rows["acme"].active and not rows["beta"].active

    new_inactive = await _run_seed(db, [_seed("gamma", active=False)])
    assert new_inactive.inserted == ["gamma"] and not (await _companies(db))["gamma"].active


async def test_companies_missing_from_file_are_kept_and_prune_deactivates(db: Database):
    await _run_seed(db, [_seed("acme"), _seed("beta"), _seed("gamma")])

    kept = await _run_seed(db, [_seed("acme")])
    assert kept.missing == ["beta", "gamma"] and kept.deactivated == []
    assert all(c.active for c in (await _companies(db)).values())

    pruned = await _run_seed(db, [_seed("acme")], prune=True)
    rows = await _companies(db)
    assert pruned.missing == ["beta", "gamma"] and pruned.deactivated == ["beta", "gamma"]
    assert len(rows) == 3 and rows["acme"].active and not rows["beta"].active and not rows["gamma"].active

    again = await _run_seed(db, [_seed("acme")], prune=True)
    assert again.deactivated == []  # already inactive: nothing to report as newly deactivated


# ---- CLI ----------------------------------------------------------------------------------------
# CliRunner is synchronous and the command calls asyncio.run, so these tests are sync and build their own
# Database on the test URL inside the patched runtime instead of using the async `db` fixture.


def patch_runtime(monkeypatch, database_url: str, tmp_path: Path) -> None:
    @asynccontextmanager
    async def fake_runtime(settings=None):
        database = Database(database_url)
        await database.connect()
        try:
            config = make_config()
            router = ModelRouter(config, {}, usage_sink=database.add_usage)  # no providers: seeding needs none
            yield radar_cli.RadarRuntime(Settings(orbit_home=tmp_path), config, database, router)
        finally:
            await database.close()

    monkeypatch.setattr(radar_cli, "open_runtime", fake_runtime)


async def sql(database_url: str, statement: str) -> list:
    database = Database(database_url)
    await database.connect()
    try:
        async with database.engine.begin() as conn:
            result = await conn.execute(text(statement))
            return [tuple(r) for r in result.all()] if result.returns_rows else []
    finally:
        await database.close()


def test_cli_radar_seed(monkeypatch, tmp_path: Path, database_url: str):
    asyncio.run(sql(database_url, "TRUNCATE radar_companies RESTART IDENTITY CASCADE"))
    patch_runtime(monkeypatch, database_url, tmp_path)
    runner = CliRunner()
    n = len(load_seeds())

    first = runner.invoke(app, ["radar", "seed"])
    second = runner.invoke(app, ["radar", "seed"])

    assert first.exit_code == 0, first.output
    assert f"seeded {n} companies from {DEFAULT_SEEDS_PATH}: {n} inserted, 0 updated, 0 unchanged" in first.output
    assert second.exit_code == 0, second.output
    assert f"0 inserted, 0 updated, {n} unchanged" in second.output
    assert asyncio.run(sql(database_url, "SELECT count(*) FROM radar_companies WHERE active")) == [(n,)]

    small = tmp_path / "small.yaml"
    small.write_text(
        yaml.safe_dump({"companies": [{"slug": "cohere", "name": "Cohere Inc", "source": "ashby",
                                       "board_token": "cohere", "hq_city": "Toronto"}]}),
        encoding="utf-8",
    )
    kept = runner.invoke(app, ["radar", "seed", "--file", str(small)])
    pruned = runner.invoke(app, ["radar", "seed", "--file", str(small), "--prune"])

    assert kept.exit_code == 0, kept.output
    assert "0 inserted, 1 updated, 0 unchanged" in kept.output
    assert "in the DB but not in the file (left as is; --prune deactivates): 1password" in kept.output
    assert pruned.exit_code == 0, pruned.output
    assert "deactivated (not in file): 1password" in pruned.output
    rows = asyncio.run(sql(database_url, "SELECT slug, name FROM radar_companies WHERE active"))
    assert rows == [("cohere", "Cohere Inc")]


def test_cli_radar_seed_rejects_bad_file_before_touching_db(monkeypatch, tmp_path: Path):
    def _no_runtime(*a, **kw):
        raise AssertionError("seed must validate the file before opening the DB")

    monkeypatch.setattr(radar_cli, "open_runtime", _no_runtime)
    bad = tmp_path / "bad.yaml"
    bad.write_text("companies:\n  - {slug: x, name: X, source: workday, board_token: x}\n", encoding="utf-8")
    runner = CliRunner()

    invalid = runner.invoke(app, ["radar", "seed", "--file", str(bad)])
    missing = runner.invoke(app, ["radar", "seed", "--file", str(tmp_path / "nope.yaml")])

    assert invalid.exit_code == 1 and "unknown source 'workday'" in invalid.output
    assert missing.exit_code == 1 and "nope.yaml" in missing.output
