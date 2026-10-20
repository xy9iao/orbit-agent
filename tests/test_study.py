"""The study pack (packs/study, used as written) and the skill change log (list / revert)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from orbit.core.tools import registry
from orbit.core.tools.registry import ToolContext
from orbit.cron import cron_trigger, crontab_weekdays
from orbit.packs import Pack, load_pack, load_packs, records, skills
from orbit.scheduler.scheduler import OrbitScheduler
from tests.conftest import ROOT
from tests.test_packs import FakeOrbit, make_agent

STUDY = ROOT / "packs" / "study"
BLOCKS = {"date": "2026-10-20", "blocks": 3, "target": 3, "courses": ["CS341", "CS350"],
          "kinds": ["catchup", "assignment"], "energy": 4}


@pytest.fixture
def packs(tmp_path: Path) -> dict[str, Pack]:
    """A copy of the repo's packs, so skill writes never touch the real files."""
    shutil.copytree(ROOT / "packs", tmp_path / "packs", ignore=shutil.ignore_patterns(".history"))
    return load_packs(tmp_path / "packs", ["fitness", "nutrition", "study"])


def ctx(packs: dict[str, Pack], tmp_path: Path, db=None) -> ToolContext:
    return ToolContext(channel="cli", chat_id="local", db=db, memory=None, packs=packs,
                       skill_changes=tmp_path / "data" / skills.CHANGES_FILE)


async def call(name: str, args: dict, c: ToolContext):
    return await registry.call(name, args, c)


# ---- the pack ------------------------------------------------------------------------------------------


def test_study_pack_loads_as_written():
    raw = yaml.safe_load((STUDY / "pack.yaml").read_text(encoding="utf-8"))
    study = load_pack(STUDY)

    assert study.name == "study"
    assert set(study.collections) == {"study_blocks", "lectures"}
    assert study.locked_sections == raw["locked_sections"] == ["目标", "每周节奏", "不做"]
    assert [r.cron for r in study.routines] == [r["cron"] for r in raw["routines"]]
    headings = skills.section_names(skills.read_skill(study))
    assert set(study.locked_sections) <= set(headings)


async def test_study_blocks_validate_on_add_record(db, packs):
    study = packs["study"]

    saved = await records.add_record(db, study, "study_blocks", BLOCKS)
    assert saved["data"] == BLOCKS

    for bad, why in [
        ({"date": "2026-10-20"}, "'blocks' is a required property"),
        ({**BLOCKS, "blocks": -1}, "blocks: -1 is less than the minimum of 0"),
        ({**BLOCKS, "date": "2026-13-45"}, "date: '2026-13-45' is not a 'date'"),
        ({**BLOCKS, "kinds": ["gaming"]}, "kinds.0: 'gaming' is not one of"),
        ({**BLOCKS, "energy": 6}, "energy: 6 is greater than the maximum of 5"),
    ]:
        with pytest.raises(records.RecordError, match="data does not match the schema") as err:
            await records.add_record(db, study, "study_blocks", bad)
        assert why in str(err.value)
    assert len(await records.query_records(db, "study", "study_blocks")) == 1


async def test_lectures_status_is_an_enum(db, packs):
    study = packs["study"]
    statuses = study.collections["lectures"]["properties"]["status"]["enum"]

    for status in statuses:
        await records.add_record(db, study, "lectures", {"course": "CS341", "lecture": "L5", "status": status})
    with pytest.raises(records.RecordError, match="status: 'kinda' is not one of"):
        await records.add_record(db, study, "lectures", {"course": "CS341", "lecture": "L5", "status": "kinda"})
    with pytest.raises(records.RecordError, match="'status' is a required property"):
        await records.add_record(db, study, "lectures", {"course": "CS341", "lecture": "L5"})
    assert len(await records.query_records(db, "study", "lectures")) == len(statuses)


async def test_study_routines_are_scheduled_on_the_right_days(db, memory, tmp_path, packs):
    agent, _ = make_agent(db, memory, tmp_path, packs, [])
    scheduler = OrbitScheduler(FakeOrbit(agent, db, packs))

    assert scheduler.register_routines() == 5
    jobs = {j.id: j for j in scheduler.sched.get_jobs()}
    preview, weekly = jobs["routine-study-0"].trigger, jobs["routine-study-1"].trigger

    def fields(trigger) -> tuple[str, str, str]:
        f = {x.name: str(x) for x in trigger.fields}
        return f["day_of_week"], f["hour"], f["minute"]

    assert fields(preview) == ("mon,wed", "19", "0")
    assert fields(weekly) == ("sun", "20", "0")
    tz = scheduler.sched.timezone
    after = datetime(2026, 10, 20, 12, tzinfo=tz)  # a Tuesday
    assert preview.get_next_fire_time(None, after).strftime("%a %H:%M") == "Wed 19:00"
    assert weekly.get_next_fire_time(None, after).strftime("%a %Y-%m-%d %H:%M") == "Sun 2026-10-25 20:00"


@pytest.mark.parametrize(
    ("field", "expected"),
    [("*", "*"), ("1,3", "mon,wed"), ("0", "sun"), ("7", "sun"), ("1-5", "mon,tue,wed,thu,fri"),
     ("0,6", "sun,sat"), ("mon-fri", "mon-fri"), ("tue-sun", "tue-sun"), ("1-7/3", "mon,thu,sun")],
)
def test_crontab_weekday_numbers_mean_what_they_mean_in_crontab(field, expected):
    assert crontab_weekdays(field) == expected


def test_cron_trigger_rejects_bad_expressions():
    for bad in ("0 19 * *", "0 19 * * 8", "0 19 * * 5-1"):
        with pytest.raises(ValueError):
            cron_trigger(bad)


# ---- skill change log ----------------------------------------------------------------------------------


async def test_write_skill_is_logged_listed_and_reverted(packs, tmp_path):
    c = ctx(packs, tmp_path)
    study = packs["study"]
    original = skills.read_skill(study)
    old_body = skills.section_body(original, "学习块")

    result = await call("write_skill", {"pack": "study", "section": "学习块", "new_text": "- 1 块 = 40 分钟",
                                        "reason": "用户说 50 分钟太长"}, c)
    assert result == "updated section '学习块' in study/SKILL.md"
    added = await call("write_skill", {"pack": "fitness", "section": "器械偏好", "new_text": "- 史密斯机"}, c)
    assert added.startswith("added section")

    lines = [json.loads(ln) for ln in c.skill_changes.read_text(encoding="utf-8").splitlines()]
    assert set(lines[0]) == {"id", "time", "pack", "section", "reason", "old", "new"}
    assert (lines[0]["id"], lines[0]["pack"], lines[0]["section"], lines[0]["reason"]) == (
        1, "study", "学习块", "用户说 50 分钟太长"
    )
    assert lines[0]["old"] == old_body
    assert lines[0]["new"] == "✏️ agent 可改\n- 1 块 = 40 分钟"  # the marker line is kept
    assert lines[1]["old"] is None and lines[1]["new"] == "- 史密斯机"

    listed = json.loads(await call("list_skill_changes", {}, c))
    assert [x["id"] for x in listed] == [2, 1]  # newest first
    assert await call("list_skill_changes", {"since": "2999-01-01"}, c) == "no skill changes"

    assert await call("revert_skill_change", {"change_id": 1}, c) == (
        "reverted change #1: section '学习块' in study/SKILL.md"
    )
    assert skills.read_skill(study) == original
    assert await call("revert_skill_change", {"change_id": 2}, c) == (
        "reverted change #2: section '器械偏好' in fitness/SKILL.md"
    )
    assert "器械偏好" not in skills.section_names(skills.read_skill(packs["fitness"]))

    reverts = skills.read_changes(c.skill_changes)[2:]
    assert [(r["id"], r["reason"], r["reverts"]) for r in reverts] == [(3, "revert #1", 1), (4, "revert #2", 2)]
    assert reverts[0]["old"] == lines[0]["new"] and reverts[0]["new"] == old_body


async def test_revert_refuses_when_the_section_changed_again(packs, tmp_path):
    c = ctx(packs, tmp_path)
    for text in ("- 第一版", "- 第二版"):
        await call("write_skill", {"pack": "study", "section": "回看节奏", "new_text": text}, c)
    before = skills.read_skill(packs["study"])

    assert await call("revert_skill_change", {"change_id": 1}, c) == (
        "error: section '回看节奏' changed again later (#2); revert those first"
    )
    assert await call("revert_skill_change", {"change_id": 99}, c) == "error: no skill change #99"
    assert skills.read_skill(packs["study"]) == before
    assert await call("revert_skill_change", {"change_id": 2}, c) == (
        "reverted change #2: section '回看节奏' in study/SKILL.md"
    )


async def test_locked_sections_stay_refused_and_are_not_logged(packs, tmp_path):
    c = ctx(packs, tmp_path)
    study = packs["study"]
    before = skills.read_skill(study)

    for section in study.locked_sections:
        result = await call("write_skill", {"pack": "study", "section": section, "new_text": "随便学。"}, c)
        assert result.startswith(f"error: section '{section}' is locked")
    assert skills.read_skill(study) == before
    assert not c.skill_changes.exists()

    # a change logged before a section was locked cannot be reverted into it either
    skills.append_changes(c.skill_changes, [{"id": 1, "time": "2026-10-20T10:00:00", "pack": "study",
                                             "section": "目标", "reason": "", "old": "x", "new": "y"}])
    assert (await call("revert_skill_change", {"change_id": 1}, c)).startswith("error: section '目标' is locked")
    assert skills.read_skill(study) == before
