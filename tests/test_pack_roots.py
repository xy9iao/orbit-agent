"""Public (packs/) and private (ORBIT_PRIVATE_PACKS) pack roots, and the public habits example pack."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from orbit.config import Settings
from orbit.export import export_snapshot, import_snapshot
from orbit.packs import load_pack, load_packs, pack_dirs, records, skills
from tests.conftest import FIXTURE_PACKS, ROOT, make_config


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An ORBIT_HOME with the public habits pack and the fixture packs as private ones."""
    home = tmp_path / "home"
    shutil.copytree(ROOT / "packs" / "habits", home / "packs" / "habits")
    shutil.copytree(FIXTURE_PACKS, home / "private-packs", ignore=shutil.ignore_patterns("README.md"))
    return home


@pytest.fixture
def info_logs():
    from loguru import logger

    messages: list[str] = []
    handler = logger.add(lambda m: messages.append(str(m)), level="INFO")
    yield messages
    logger.remove(handler)


def settings_for(home: Path, database_url: str = "postgresql+asyncpg://x@localhost/x", **kw) -> Settings:
    return Settings(orbit_home=home, database_url=database_url, export_dir=Path("data/exports"), **kw)


def test_private_root_defaults_under_orbit_home_and_follows_the_env(tmp_path, monkeypatch):
    assert settings_for(tmp_path).private_packs_path == tmp_path / "private-packs"
    monkeypatch.setenv("ORBIT_PRIVATE_PACKS", "mine")
    assert Settings(orbit_home=tmp_path).private_packs_path == tmp_path / "mine"
    monkeypatch.setenv("ORBIT_PRIVATE_PACKS", str(tmp_path / "elsewhere"))
    assert Settings(orbit_home=tmp_path).pack_roots(make_config().packs) == [
        tmp_path / "packs", tmp_path / "elsewhere"
    ]


def test_packs_load_from_both_roots(home):
    roots = settings_for(home).pack_roots(make_config().packs)

    packs = load_packs(roots, ["habits", "fitness", "study"])

    assert list(packs) == ["habits", "fitness", "study"]
    assert packs["habits"].dir == home / "packs" / "habits"
    assert packs["fitness"].dir == home / "private-packs" / "fitness"
    assert set(pack_dirs(roots)) == {"habits", "fitness", "nutrition", "study"}


def test_a_private_pack_overrides_a_public_one_with_the_same_name(home, info_logs):
    private = home / "private-packs" / "habits"
    shutil.copytree(home / "packs" / "habits", private)
    yaml_file = private / "pack.yaml"
    yaml_file.write_text(
        yaml_file.read_text(encoding="utf-8").replace("description: Daily habit tracker.", "description: Mine."),
        encoding="utf-8",
    )
    roots = settings_for(home).pack_roots(make_config().packs)

    habits = load_packs(roots, ["habits"])["habits"]

    assert habits.dir == private and habits.description.startswith("Mine.")
    assert pack_dirs(roots)["habits"] == private
    assert any("pack 'habits':" in m and "overrides" in m for m in info_logs)


def test_write_skill_history_and_change_log_work_in_the_private_root(home):
    settings = settings_for(home)
    study = load_packs(settings.pack_roots(make_config().packs), ["study"])["study"]

    result = skills.write_skill(study, "回看节奏", "- 每周日", reason="test", log=settings.skill_changes_path)

    assert result == "updated section '回看节奏' in study/SKILL.md"
    assert list((home / "private-packs" / "study" / ".history").glob("SKILL.*.md"))
    assert skills.read_changes(settings.skill_changes_path)[0]["pack"] == "study"
    assert skills.revert_change({"study": study}, settings.skill_changes_path, 1).startswith("reverted change #1")


async def test_export_records_each_pack_root_and_import_restores_into_it(home, db, database_url, tmp_path):
    settings = settings_for(home, database_url)
    config = make_config()
    out = tmp_path / "snapshot"

    manifest = await export_snapshot(db, settings, config, out)

    assert manifest["pack_roots"] == {"fitness": "private", "habits": "public", "nutrition": "private",
                                      "study": "private"}
    assert (out / "skills" / "study" / "SKILL.md").is_file() and (out / "packs" / "habits" / "pack.yaml").is_file()

    fresh = tmp_path / "fresh"
    result = await import_snapshot(db, settings_for(fresh, database_url), config, out)

    assert result["packs"] == 4 and result["skills"] == 4
    assert (fresh / "packs" / "habits" / "SKILL.md").is_file()
    for name in ("fitness", "nutrition", "study"):
        assert (fresh / "private-packs" / name / "SKILL.md").is_file()
        assert not (fresh / "packs" / name).exists()  # never into the public (git) root


async def test_an_export_without_pack_roots_restores_into_the_private_root(home, db, database_url, tmp_path):
    settings = settings_for(home, database_url)
    out = tmp_path / "snapshot"
    await export_snapshot(db, settings, make_config(), out)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    del manifest["pack_roots"]  # an export made before there were two roots
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    fresh = tmp_path / "fresh"
    await import_snapshot(db, settings_for(fresh, database_url), make_config(), out)

    assert (fresh / "private-packs" / "habits" / "pack.yaml").is_file()
    assert not (fresh / "packs").exists()


# ---- the public example pack ---------------------------------------------------------------------------


async def test_the_habits_example_pack_loads_and_validates_checkins(db):
    habits = load_pack(ROOT / "packs" / "habits")

    assert set(habits.collections) == {"checkins"}
    assert [r.cron for r in habits.routines] == ["0 21 * * *"]
    assert set(habits.locked_sections) <= set(skills.section_names(skills.read_skill(habits)))

    saved = await records.add_record(db, habits, "checkins", {"habit": "read", "done": True, "note": "20 min"})
    assert saved["data"]["habit"] == "read"
    for bad in ({"habit": "read"}, {"habit": "", "done": True}, {"habit": "read", "done": "yes"}):
        with pytest.raises(records.RecordError):
            await records.add_record(db, habits, "checkins", bad)


def test_only_the_example_pack_is_public():
    assert set(pack_dirs(ROOT / "packs")) == {"habits"}
