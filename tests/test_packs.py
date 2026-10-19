"""Domain packs: loading, record validation and stats, skill editing, routines, photos, export/import."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, text

from orbit.channels.telegram import TelegramChannel
from orbit.config import Settings
from orbit.core.agent import Agent
from orbit.core.providers.anthropic import convert_messages
from orbit.core.providers.base import Response
from orbit.core.router import ModelRouter
from orbit.core.tools import registry
from orbit.core.tools.registry import ToolContext
from orbit.export import export_snapshot, import_snapshot, prune
from orbit.packs import Pack, Routine, load_pack, load_packs, records, skills
from orbit.scheduler.jobs.routines import run_routine
from orbit.scheduler.scheduler import OrbitScheduler
from orbit.storage.db import Database
from orbit.storage.models import Record
from tests.conftest import ROOT, FakeProvider, make_config, tool_call_response

BENCH = {
    "day_type": "chest",
    "exercise": "卧推",
    "sets": [
        {"weight": 80, "unit": "kg", "reps": 8, "rir": 2},
        {"weight": 80, "unit": "kg", "reps": 7, "rir": 1},
    ],
    "energy": 4,
}


@pytest.fixture
def packs_dir(tmp_path: Path) -> Path:
    """A copy of the repo's packs, so skill writes never touch the real files."""
    target = tmp_path / "packs"
    shutil.copytree(ROOT / "packs", target, ignore=shutil.ignore_patterns(".history"))
    return target


@pytest.fixture
def packs(packs_dir: Path) -> dict[str, Pack]:
    return load_packs(packs_dir, ["fitness", "nutrition"])


def ctx(db: Database, packs: dict[str, Pack], memory=None) -> ToolContext:
    return ToolContext(channel="cli", chat_id="local", db=db, memory=memory, packs=packs)


# ---- loading -------------------------------------------------------------------------------------------


def test_repo_packs_load_with_their_collections_routines_and_locks(packs):
    fitness, nutrition = packs["fitness"], packs["nutrition"]
    assert set(fitness.collections) == {"workouts"} and set(nutrition.collections) == {"meals"}
    assert [r.cron for r in fitness.routines] == ["30 7 * * *", "0 21 * * *"]
    assert [r.cron for r in nutrition.routines] == ["30 21 * * *"]
    assert fitness.locked_sections == ["[redacted]"]
    assert fitness.schema_version == 1 and nutrition.schema_version == 1
    sections = skills.section_names(skills.read_skill(fitness))
    assert sections == ["[redacted]", "分化", "训练方法", "记录格式", "沟通偏好"]
    assert skills.section_names(skills.read_skill(nutrition)) == ["目标", "估算规则", "记录格式", "沟通偏好"]


def test_repo_config_enables_both_packs():
    from orbit.config import load_config

    assert load_config(ROOT / "config.yaml").packs.enabled == ["fitness", "nutrition"]


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda d: (d / "pack.yaml").write_text("name: [unclosed", encoding="utf-8"), "invalid YAML"),
        (lambda d: (d / "SKILL.md").unlink(), "SKILL.md not found"),
        (
            lambda d: (d / "pack.yaml").write_text(
                "name: broken\ndescription: x\ncollections:\n  c: {type: nonsense}\n", encoding="utf-8"
            ),
            "invalid schema",
        ),
        (
            lambda d: (d / "pack.yaml").write_text(
                "name: broken\ndescription: x\nroutines:\n  - {cron: 'not a cron', prompt: hi}\n", encoding="utf-8"
            ),
            "cron",
        ),
        (lambda d: (d / "pack.yaml").write_text("name: other\ndescription: x\n", encoding="utf-8"), "folder name"),
    ],
)
def test_broken_pack_is_logged_and_skipped(packs_dir: Path, mutate, reason, caplog_loguru):
    broken = packs_dir / "broken"
    broken.mkdir()
    (broken / "pack.yaml").write_text("name: broken\ndescription: ok\n", encoding="utf-8")
    (broken / "SKILL.md").write_text("## a\nb\n", encoding="utf-8")
    assert load_pack(broken).name == "broken"  # valid before the mutation
    mutate(broken)

    loaded = load_packs(packs_dir, ["fitness", "broken", "nutrition", "missing"])

    assert list(loaded) == ["fitness", "nutrition"]
    errors = [m for m in caplog_loguru if "pack 'broken' failed to load" in m]
    assert errors and reason in errors[0]
    assert any("pack 'missing' failed to load" in m for m in caplog_loguru)


@pytest.fixture
def caplog_loguru():
    from loguru import logger

    messages: list[str] = []
    handler = logger.add(lambda m: messages.append(str(m)), level="ERROR")
    yield messages
    logger.remove(handler)


# ---- records -------------------------------------------------------------------------------------------


async def test_add_record_validates_against_the_pack_schema(db, packs):
    c = ctx(db, packs)
    saved = await registry.call(
        "add_record",
        {"pack": "fitness", "collection": "workouts", "data": BENCH, "occurred_at": "2026-10-20T18:30"},
        c,
    )
    row = json.loads(saved)
    assert row["occurred_at"] == "2026-10-20T18:30" and row["data"] == BENCH

    bad = {**BENCH, "day_type": "arms", "sets": [{"weight": "heavy", "unit": "kg", "reps": 8}]}
    error = await registry.call("add_record", {"pack": "fitness", "collection": "workouts", "data": bad}, c)
    assert error.startswith("error: data does not match the schema")
    assert "day_type: 'arms' is not one of" in error
    assert "sets.0.weight: 'heavy' is not of type 'number'" in error
    meal = {"meal": "lunch", "items": [{"name": "x"}], "protein_g": 1, "mood": "ok"}
    extra = await registry.call("add_record", {"pack": "nutrition", "collection": "meals", "data": meal}, c)
    assert "Additional properties are not allowed ('mood'" in extra

    assert "unknown pack 'yoga'" in await registry.call(
        "add_record", {"pack": "yoga", "collection": "x", "data": {}}, c
    )
    assert "has no collection 'runs'" in await registry.call(
        "add_record", {"pack": "fitness", "collection": "runs", "data": BENCH}, c
    )
    async with db.session() as s:
        stored = (await s.execute(select(Record))).scalars().all()
    assert len(stored) == 1 and stored[0].schema_version == 1


async def test_query_update_and_delete_records(db, packs):
    c = ctx(db, packs)
    pack = packs["fitness"]
    first = await records.add_record(db, pack, "workouts", BENCH, datetime(2026, 10, 13, 18))
    await records.add_record(db, pack, "workouts", {**BENCH, "exercise": "深蹲", "day_type": "legs"},
                             datetime(2026, 10, 14, 18))
    await records.add_record(db, pack, "workouts", BENCH, datetime(2026, 10, 20, 18))

    rows = json.loads(await registry.call("query_records", {"pack": "fitness", "collection": "workouts"}, c))
    assert [r["occurred_at"][:10] for r in rows] == ["2026-10-20", "2026-10-14", "2026-10-13"]  # newest first
    bench = json.loads(
        await registry.call(
            "query_records",
            {"pack": "fitness", "collection": "workouts", "contains": "卧推", "until": "2026-10-20"},
            c,
        )
    )
    assert [r["id"] for r in bench] == [first["id"]]
    assert len(await records.query_records(db, "fitness", "workouts", limit=1)) == 1
    assert len(await records.query_records(db, "fitness", "workouts", limit=10_000)) == 3  # capped at 200

    updated = json.loads(
        await registry.call("update_record", {"record_id": first["id"], "data": {"pain": 2, "energy": None}}, c)
    )
    assert updated["data"]["pain"] == 2 and "energy" not in updated["data"]
    rejected = await registry.call("update_record", {"record_id": first["id"], "data": {"pain": 11}}, c)
    assert rejected.startswith("error:") and "pain: 11 is greater than the maximum of 10" in rejected

    assert await registry.call("delete_record", {"record_id": first["id"]}, c) == f"deleted record {first['id']}"
    assert await registry.call("delete_record", {"record_id": first["id"]}, c) == "error: no such record"


def test_compute_stats_groups_and_flattens_lists():
    rows = [
        (datetime(2026, 10, 19, 8), {"meal": "breakfast", "protein_g": 30}),
        (datetime(2026, 10, 19, 13), {"meal": "lunch", "protein_g": 50.5}),
        (datetime(2026, 10, 20, 8), {"meal": "breakfast", "protein_g": 25}),
        (datetime(2026, 10, 20, 9), {"meal": "snack"}),  # no value: ignored
    ]
    assert records.compute_stats(rows, "protein_g", "day") == [
        {"group": "2026-10-19", "count": 2, "sum": 80.5, "min": 30, "max": 50.5, "first": 30, "last": 50.5},
        {"group": "2026-10-20", "count": 1, "sum": 25, "min": 25, "max": 25, "first": 25, "last": 25},
    ]
    assert [g["group"] for g in records.compute_stats(rows, "protein_g", "meal")] == ["breakfast", "lunch"]
    total = records.compute_stats(rows, "protein_g")
    assert total == [{"group": "all", "count": 3, "sum": 105.5, "min": 25, "max": 50.5, "first": 30, "last": 25}]
    week = records.compute_stats(rows, "protein_g", "week")
    assert [g["group"] for g in week] == ["2026-10-19"]  # both days are in the week of Monday 2026-10-19

    sets = [(datetime(2026, 10, 1), {"exercise": "卧推", "sets": [{"weight": 75}, {"weight": 80}]})]
    assert records.compute_stats(sets, "sets.weight", "exercise")[0] == {
        "group": "卧推", "count": 2, "sum": 155, "min": 75, "max": 80, "first": 75, "last": 80
    }


async def test_record_stats_tool_reads_the_database(db, packs):
    c = ctx(db, packs)
    meal = {"meal": "lunch", "items": [{"name": "鸡胸", "amount": "200 g", "protein_g": 46}], "protein_g": 46}
    await records.add_record(db, packs["nutrition"], "meals", meal, datetime(2026, 10, 18, 12))
    await records.add_record(db, packs["nutrition"], "meals", {**meal, "protein_g": 60}, datetime(2026, 10, 19, 12))
    await records.add_record(db, packs["nutrition"], "meals", {**meal, "protein_g": 40}, datetime(2026, 10, 19, 19))

    stats = json.loads(
        await registry.call(
            "record_stats",
            {"pack": "nutrition", "collection": "meals", "field": "protein_g", "group_by": "day",
             "since": "2026-10-19"},
            c,
        )
    )
    assert stats == [{"group": "2026-10-19", "count": 2, "sum": 100, "min": 40, "max": 60, "first": 60, "last": 40}]
    items = json.loads(
        await registry.call(
            "record_stats", {"pack": "nutrition", "collection": "meals", "field": "items.protein_g"}, c
        )
    )
    assert items[0]["count"] == 3
    assert await registry.call(
        "record_stats", {"pack": "nutrition", "collection": "meals", "field": "fiber_g"}, c
    ) == "no values for that field"


# ---- skills --------------------------------------------------------------------------------------------


def test_write_skill_replaces_one_section_and_keeps_history(packs):
    fitness = packs["fitness"]
    before = skills.read_skill(fitness)
    c = ToolContext(channel="cli", chat_id="local", db=None, memory=None, packs=packs)

    args = {"pack": "fitness", "section": "沟通偏好", "new_text": "- 只用中文。\n- 不超过五行。"}
    result = registry_call_sync("write_skill", args, c)

    assert result == "updated section '沟通偏好' in fitness/SKILL.md"
    after = skills.read_skill(fitness)
    assert "- 只用中文。\n- 不超过五行。" in after
    assert skills.section_names(after) == skills.section_names(before)
    for other in ("[redacted]", "分化", "训练方法", "记录格式"):
        assert _section(after, other) == _section(before, other)
    history = list(fitness.history_dir.glob("SKILL.*.md"))
    assert len(history) == 1 and history[0].read_text(encoding="utf-8") == before

    added = skills.write_skill(fitness, "器械偏好", "史密斯机代替杠铃深蹲。")
    assert added == "added section '器械偏好' in fitness/SKILL.md"
    assert skills.section_names(skills.read_skill(fitness))[-1] == "器械偏好"
    assert len(list(fitness.history_dir.glob("SKILL.*.md"))) == 2


def test_write_skill_refuses_locked_sections_and_headings(packs):
    fitness = packs["fitness"]
    before = skills.read_skill(fitness)

    locked = skills.write_skill(fitness, "[redacted]", "随便练。")
    assert locked.startswith("error: section '[redacted]' is locked")
    injected = skills.write_skill(fitness, "训练方法", "加重。\n## [redacted]\n随便练。")
    assert injected.startswith("error: new_text must not contain")

    assert skills.read_skill(fitness) == before
    assert not fitness.history_dir.exists()


def test_load_skill_tool_returns_skill_md(packs):
    c = ToolContext(channel="cli", chat_id="local", db=None, memory=None, packs=packs)
    assert registry_call_sync("load_skill", {"pack": "nutrition"}, c).startswith("# nutrition\n\n## 目标")


def _section(text_: str, name: str) -> str:
    lines = text_.splitlines()
    start = lines.index(f"## {name}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip()


def registry_call_sync(name: str, args: dict[str, Any], c: ToolContext) -> str:
    import asyncio

    return asyncio.run(registry.call(name, args, c))


# ---- agent: system prompt, routines, photos ------------------------------------------------------------


def make_agent(db, memory, tmp_path: Path, packs, responses):
    config = make_config()
    provider = FakeProvider("openai", responses)
    router = ModelRouter(config, {"openai": provider, "cheap": provider})
    persona = tmp_path / "persona"
    persona.mkdir(exist_ok=True)
    (persona / "system.md").write_text("You are Orbit test.", encoding="utf-8")
    return Agent(config.agent, router, registry, memory, db, persona, packs), provider


async def test_system_prompt_lists_packs_and_logging_rules(db, memory, tmp_path, packs):
    agent, provider = make_agent(db, memory, tmp_path, packs, [Response(text="ok")])
    await agent.handle("cli", "local", "hi")
    system = provider.calls[0]["messages"][0]["content"]
    assert "- fitness: 健身训练" in system and "- nutrition: 饮食记录" in system
    assert "add_record" in system and "write_skill" in system and "load_skill" in system
    assert "## [redacted]" not in system  # skills load on demand, not in every prompt
    names = {t["function"]["name"] for t in provider.calls[0]["tools"]}
    assert {"add_record", "query_records", "update_record", "delete_record", "record_stats", "load_skill",
            "write_skill"} <= names


async def test_chat_logs_a_meal_through_add_record(db, memory, tmp_path, packs):
    meal = {"meal": "breakfast", "items": [{"name": "鸡蛋", "amount": "3 个", "protein_g": 18}], "protein_g": 18}
    agent, provider = make_agent(
        db, memory, tmp_path, packs,
        [tool_call_response("add_record", {"pack": "nutrition", "collection": "meals", "data": meal}),
         Response(text="已记早餐：鸡蛋 3 个，蛋白质 18 g")],
    )
    reply = await agent.handle("telegram", "1", "早餐吃了三个鸡蛋")
    assert reply == "已记早餐：鸡蛋 3 个，蛋白质 18 g"
    stored = await records.query_records(db, "nutrition", "meals")
    assert [r["data"] for r in stored] == [meal]


class FakeOrbit:
    """Just what the scheduler and run_routine touch."""

    def __init__(self, agent, db, packs, telegram_chat: str | None = "42"):
        self.agent = agent
        self.db = db
        self.packs = packs
        self.pushed: list[str] = []
        self.channels = {}
        if telegram_chat:
            self.channels["telegram"] = SimpleNamespace(default_chat_id=lambda: telegram_chat)
        self.config = make_config()

    async def push(self, text_: str, channel=None, chat_id=None) -> bool:
        self.pushed.append(text_)
        return True


async def test_routines_are_scheduled_in_local_time(db, memory, tmp_path, packs):
    agent, _ = make_agent(db, memory, tmp_path, packs, [])
    scheduler = OrbitScheduler(FakeOrbit(agent, db, packs))
    assert scheduler.register_routines() == 3
    jobs = {j.id: j for j in scheduler.sched.get_jobs()}
    assert set(jobs) == {"routine-fitness-0", "routine-fitness-1", "routine-nutrition-0"}
    trigger = jobs["routine-fitness-0"].trigger
    assert str(trigger.timezone) == str(scheduler.sched.timezone)
    fields = {f.name: str(f) for f in trigger.fields}
    assert (fields["hour"], fields["minute"], fields["day_of_week"]) == ("7", "30", "*")
    nutrition = {f.name: str(f) for f in jobs["routine-nutrition-0"].trigger.fields}
    assert (nutrition["hour"], nutrition["minute"]) == ("21", "30")


async def test_routine_fire_runs_the_agent_with_the_skill_and_pushes(db, memory, tmp_path, packs):
    meal = {"meal": "lunch", "items": [{"name": "鸡胸"}], "protein_g": 90}
    await records.add_record(db, packs["nutrition"], "meals", meal)
    agent, provider = make_agent(
        db, memory, tmp_path, packs,
        [tool_call_response("record_stats", {"pack": "nutrition", "collection": "meals", "field": "protein_g",
                                             "group_by": "day"}),
         Response(text="今天蛋白质 90 g，还差 50 g：睡前一杯希腊酸奶加一勺蛋白粉。")],
    )
    orbit = FakeOrbit(agent, db, packs)
    scheduler = OrbitScheduler(orbit)

    await scheduler.fire_routine("nutrition", packs["nutrition"].routines[0])

    assert orbit.pushed == ["今天蛋白质 90 g，还差 50 g：睡前一杯希腊酸奶加一勺蛋白粉。"]
    first = provider.calls[0]["messages"]
    assert "## 当前任务使用的 skill：nutrition" in first[0]["content"] and "## 估算规则" in first[0]["content"]
    assert first[1]["content"].startswith("[定时任务 · nutrition] 今天的蛋白质检查")
    assert provider.calls[0]["tools"]  # the full tool loop, not a one-shot generation
    assert '"sum": 90' in provider.calls[1]["messages"][-1]["content"]
    history = await db.get_history("telegram:42", 10)
    assert history[-1] == {"role": "assistant", "content": orbit.pushed[0]}


async def test_routine_that_has_nothing_to_say_pushes_nothing(db, memory, tmp_path, packs):
    agent, provider = make_agent(db, memory, tmp_path, packs, [Response(text="NO_MESSAGE")])
    orbit = FakeOrbit(agent, db, packs)

    assert await run_routine(orbit, "fitness", packs["fitness"].routines[1]) == ""

    assert orbit.pushed == []
    assert await db.get_history("telegram:42", 10) == []
    assert provider.calls[0]["messages"][1]["content"].endswith("只回复 NO_MESSAGE，不要别的字。）")


async def test_routine_without_telegram_still_pushes(db, memory, tmp_path, packs):
    agent, _ = make_agent(db, memory, tmp_path, packs, [Response(text="计划")])
    orbit = FakeOrbit(agent, db, packs, telegram_chat=None)
    assert await run_routine(orbit, "fitness", Routine("30 7 * * tue-sun", "plan")) == "计划"
    assert orbit.pushed == ["计划"]


class FakeFile:
    def __init__(self, data: bytes):
        self.data = data

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self.data)


class FakePhotoSize:
    def __init__(self, unique_id: str, data: bytes):
        self.file_unique_id = unique_id
        self._data = data

    async def get_file(self) -> FakeFile:
        return FakeFile(self._data)


def photo_update(chat_id: int, caption: str | None, data: bytes) -> tuple[SimpleNamespace, list[str]]:
    replies: list[str] = []

    async def reply_text(t: str) -> None:
        replies.append(t)

    async def send_action(_action) -> None:
        return None

    message = SimpleNamespace(
        text=None,
        caption=caption,
        photo=[FakePhotoSize("small", b"thumb"), FakePhotoSize("AQADbig", data)],
        reply_text=reply_text,
    )
    chat = SimpleNamespace(id=chat_id, send_action=send_action)
    return SimpleNamespace(message=message, effective_chat=chat), replies


@pytest.mark.parametrize("caption", ["午饭", None])
async def test_photo_message_is_saved_and_reaches_the_model_as_an_image(db, memory, tmp_path, packs, caption):
    agent, provider = make_agent(db, memory, tmp_path, packs, [Response(text="看到了：鸡胸饭")])

    async def handler(incoming):
        return await agent.handle(incoming.channel, incoming.chat_id, incoming.text, incoming.images)

    channel = TelegramChannel("token", owner_chat_id=7, media_dir=tmp_path / "data" / "media", home=tmp_path)
    channel._handler = handler
    jpeg = b"\xff\xd8\xff\xe0fake-jpeg"
    update, replies = photo_update(7, caption, jpeg)

    await channel._on_photo(update, None)

    saved = list((tmp_path / "data" / "media").glob("*/*.jpg"))
    assert len(saved) == 1 and saved[0].read_bytes() == jpeg
    assert saved[0].parent.name == datetime.now().strftime("%Y-%m-%d") and saved[0].name.endswith("_AQADbig.jpg")
    assert replies == ["看到了：鸡胸饭"]

    user = provider.calls[0]["messages"][-1]
    rel = saved[0].relative_to(tmp_path).as_posix()
    expected_text = f"{caption}\n[照片已保存：{rel}]" if caption else f"[照片已保存：{rel}]"
    assert user["content"][0] == {"type": "text", "text": expected_text}
    image = user["content"][1]
    assert image["type"] == "image_url" and image["image_url"]["url"].startswith("data:image/jpeg;base64,")

    # Claude gets it as an image block
    _, converted = convert_messages([user])
    assert converted[0]["content"][1]["type"] == "image"
    assert converted[0]["content"][1]["source"]["media_type"] == "image/jpeg"
    # history keeps the text only
    assert (await db.get_history("telegram:7", 5))[0]["content"] == expected_text


async def test_photo_from_a_stranger_is_ignored(tmp_path):
    channel = TelegramChannel("token", owner_chat_id=7, media_dir=tmp_path / "media")

    async def handler(_):
        raise AssertionError("must not be called")

    channel._handler = handler
    update, replies = photo_update(999, "hi", b"x")
    await channel._on_photo(update, None)
    assert replies == [] and not (tmp_path / "media").exists()


def test_text_messages_are_unchanged_by_images_support():
    _, converted = convert_messages([{"role": "user", "content": "卧推 80kg 8 8 7"}])
    assert converted == [{"role": "user", "content": [{"type": "text", "text": "卧推 80kg 8 8 7"}]}]


# ---- export / import -----------------------------------------------------------------------------------


def home_settings(home: Path, database_url: str) -> Settings:
    return Settings(orbit_home=home, database_url=database_url, export_dir=Path("data/exports"))


async def test_export_then_import_into_an_empty_db_gives_identical_records(db, database_url, tmp_path, packs_dir):
    home = tmp_path / "home"
    shutil.copytree(packs_dir, home / "packs")
    (home / "persona").mkdir()
    (home / "persona" / "system.md").write_text("persona", encoding="utf-8")
    (home / "memory").mkdir()
    (home / "memory" / "MEMORY.md").write_text("# Orbit Memory\n\n- [2026-10-19] likes tea\n", encoding="utf-8")
    photo = home / "data" / "media" / "2026-10-19" / "120000_abc.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"jpeg")
    settings = home_settings(home, database_url)
    config = make_config()
    config.packs.enabled = ["fitness", "nutrition"]
    loaded = load_packs(home / "packs", config.packs.enabled)
    photo_ref = "data/media/2026-10-19/120000_abc.jpg"
    meal = {"meal": "dinner", "items": [{"name": "三文鱼"}], "protein_g": 40, "photo": photo_ref}
    await records.add_record(db, loaded["fitness"], "workouts", BENCH, datetime(2026, 10, 19, 18))
    await records.add_record(db, loaded["nutrition"], "meals", meal, datetime(2026, 10, 19, 19))
    await db.add_message("telegram:1", "user", "hi")
    await db.add_reminder("telegram", "1", datetime(2027, 1, 1, 9), "new year")
    before = await _all_records(db)

    out = tmp_path / "snapshot"
    manifest = await export_snapshot(db, settings, config, out)

    assert manifest["counts"] == {
        "records": {"fitness/workouts": 1, "nutrition/meals": 1},
        "reminders": 1,
        "messages": 1,
        "media_files": 1,
    }
    assert manifest["schemas"]["fitness"]["schema_version"] == 1
    assert "workouts" in manifest["schemas"]["fitness"]["collections"]
    for rel in ("README.md", "manifest.json", "records/fitness/workouts.jsonl", "records/nutrition/meals.jsonl",
                "skills/fitness/SKILL.md", "packs/nutrition/pack.yaml", "memory/MEMORY.md", "persona/system.md",
                "reminders.jsonl", "messages.jsonl", "media/2026-10-19/120000_abc.jpg"):
        assert (out / rel).is_file(), rel
    line = json.loads((out / "records/nutrition/meals.jsonl").read_text(encoding="utf-8"))
    assert line["occurred_at"] == "2026-10-19T19:00:00" and line["data"] == meal  # plain JSON, no Orbit needed

    # an empty database and an empty home
    async with db.engine.begin() as conn:
        await conn.execute(text("TRUNCATE records RESTART IDENTITY"))
    new_home = tmp_path / "restored"
    new_settings = home_settings(new_home, database_url)

    result = await import_snapshot(db, new_settings, config, out)

    assert result == {"records": 2, "records_skipped": 0, "skills": 2, "packs": 2, "memory_lines": 1,
                      "media_files": 1}
    assert await _all_records(db) == before
    assert (new_home / "packs/fitness/SKILL.md").read_text(encoding="utf-8") == (
        home / "packs/fitness/SKILL.md"
    ).read_text(encoding="utf-8")
    assert "likes tea" in (new_home / "memory/MEMORY.md").read_text(encoding="utf-8")
    assert (new_home / "data/media/2026-10-19/120000_abc.jpg").read_bytes() == b"jpeg"

    again = await import_snapshot(db, new_settings, config, out)  # idempotent
    assert again == {"records": 0, "records_skipped": 2, "skills": 0, "packs": 0, "memory_lines": 0,
                     "media_files": 0}
    assert await _all_records(db) == before
    new = await records.add_record(db, loaded["fitness"], "workouts", BENCH)  # the id sequence moved past them
    assert new["id"] == 3


async def _all_records(db: Database) -> list[tuple]:
    async with db.session() as s:
        rows = (await s.execute(select(Record).order_by(Record.id))).scalars().all()
    return [(r.id, r.pack, r.collection, r.occurred_at, r.data, r.schema_version, r.created_at) for r in rows]


def test_prune_keeps_the_newest_snapshots_and_dumps(tmp_path):
    for day in range(1, 18):
        (tmp_path / f"orbit-2026-10-{day:02d}").mkdir()
        (tmp_path / f"orbit-2026-10-{day:02d}.dump").write_bytes(b"")
    (tmp_path / "manual-20261001-120000").mkdir()

    removed = prune(tmp_path, keep=14)

    assert len(removed) == 6
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left[0] == "manual-20261001-120000"
    assert [n for n in left if n.endswith(".dump")][0] == "orbit-2026-10-04.dump"
    assert len([n for n in left if n.startswith("orbit-") and not n.endswith(".dump")]) == 14


async def test_import_rejects_a_folder_that_is_not_an_export(db, database_url, tmp_path):
    with pytest.raises(FileNotFoundError, match="not an Orbit export"):
        await import_snapshot(db, home_settings(tmp_path, database_url), make_config(), tmp_path)
