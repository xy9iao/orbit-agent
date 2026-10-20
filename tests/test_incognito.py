"""Incognito mode: /incognito, /incognito off, the "??" prefix, timeout, restart, tools, photos and logs."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from orbit.app import OrbitApp
from orbit.channels.base import Incoming
from orbit.core import incognito
from orbit.core.providers.base import Response
from orbit.core.router import ModelRouter
from orbit.packs import load_packs
from orbit.storage.db import Database
from orbit.storage.models import Record
from tests.conftest import ROOT, FakeProvider, tool_call_response
from tests.test_health import make_app

SECRET = "photosynthesis-question-xyz"


@pytest.fixture
def app_and_model(tmp_path: Path, db: Database) -> tuple[OrbitApp, FakeProvider]:
    app = make_app(tmp_path, db)
    provider = FakeProvider("openai")
    app.agent.router = ModelRouter(app.config, {"openai": provider, "cheap": provider})
    shutil.copytree(ROOT / "packs", tmp_path / "packs", ignore=shutil.ignore_patterns(".history"))
    app.agent.packs = load_packs(tmp_path / "packs", ["fitness", "nutrition"])
    return app, provider


def say(text: str, **kw) -> Incoming:
    return Incoming(channel="telegram", chat_id="1", text=text, **kw)


async def history(db: Database) -> list[dict]:
    return await db.get_history("telegram:1", 100)


async def records(db: Database) -> int:
    async with db.session() as s:
        return int(await s.scalar(select(func.count()).select_from(Record)) or 0)


def test_parse():
    assert incognito.parse("/incognito") == ("on", "")
    assert incognito.parse("/incognito@OrbitBot off") == ("off", "")
    assert incognito.parse("  /INCOGNITO  关 ") == ("off", "")
    assert incognito.parse("?? 光合作用是什么") == ("once", "光合作用是什么")
    assert incognito.parse("？？translate hello") == ("once", "translate hello")
    assert incognito.parse("卧推 155 8 8 7") == (None, "卧推 155 8 8 7")
    assert incognito.parse("why??") == (None, "why??")


async def test_incognito_mode_keeps_nothing_and_threads_in_memory(app_and_model, db):
    app, model = app_and_model
    model.responses = [Response(text="光合作用把光能变成化学能。"), Response(text="叶绿体里。")]
    memory_before = app.memory.read()

    assert await app.handle(say("/incognito")) == incognito.ON_TEXT
    first = await app.handle(say(f"光合作用是什么 {SECRET}"))
    second = await app.handle(say("在哪里发生？"))

    assert first == "🕶 光合作用把光能变成化学能。" and second == "🕶 叶绿体里。"
    assert await history(db) == []  # nothing in the messages table
    assert app.memory.read() == memory_before
    turn2 = model.calls[1]["messages"]
    assert turn2[1:] == [
        {"role": "user", "content": f"光合作用是什么 {SECRET}"},
        {"role": "assistant", "content": "光合作用把光能变成化学能。"},
        {"role": "user", "content": "在哪里发生？"},
    ]
    assert "## 无痕模式" in turn2[0]["content"]
    offered = {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert offered == set(incognito.INCOGNITO_TOOLS)


async def test_writing_tools_are_refused_even_if_the_model_calls_them(app_and_model, db):
    app, model = app_and_model
    meal = {"meal": "lunch", "items": [{"name": "鸡胸"}], "protein_g": 50}
    model.responses = [
        tool_call_response("remember", {"note": "user asked about photosynthesis"}, "c1"),
        tool_call_response("add_record", {"pack": "nutrition", "collection": "meals", "data": meal}, "c2"),
        Response(text="好的"),
    ]
    memory_before = app.memory.read()

    await app.handle(say("?? 随便问问"))

    results = [m["content"] for m in model.calls[2]["messages"] if m["role"] == "tool"]
    assert results == [
        "error: tool 'remember' is not available right now",
        "error: tool 'add_record' is not available right now",
    ]
    assert app.memory.read() == memory_before
    assert await records(db) == 0


async def test_prefix_is_one_message_only_and_has_no_context(app_and_model, db):
    app, model = app_and_model
    model.responses = [Response(text="normal 1"), Response(text="Bonjour"), Response(text="normal 2")]

    await app.handle(say("早上好"))
    assert await app.handle(say("?? translate hello to French")) == "🕶 Bonjour"
    await app.handle(say("今天练什么"))

    assert [m["content"] for m in await history(db)] == ["早上好", "normal 1", "今天练什么", "normal 2"]
    once = model.calls[1]["messages"]
    assert [m["role"] for m in once] == ["system", "user"]  # no earlier history in a one-off question
    assert once[1]["content"] == "translate hello to French"


async def test_off_restores_recording_and_drops_the_buffer(app_and_model, db):
    app, model = app_and_model
    model.responses = [Response(text="a"), Response(text="b"), Response(text="c")]

    await app.handle(say("/incognito"))
    await app.handle(say("q1"))
    assert await app.handle(say("/incognito off")) == incognito.OFF_TEXT
    assert await app.handle(say("/incognito off")) == incognito.ALREADY_OFF_TEXT
    assert await app.handle(say("q2")) == "b"
    assert await app.handle(say("/incognito")) == incognito.ON_TEXT
    await app.handle(say("q3"))

    assert [m["content"] for m in await history(db)] == ["q2", "b"]
    assert [m["content"] for m in model.calls[2]["messages"][1:]] == ["q3"]  # the old buffer is gone


async def test_idle_timeout_turns_it_off_and_says_so(app_and_model, db):
    app, model = app_and_model
    model.responses = [Response(text="x"), Response(text="y")]
    now = [datetime(2026, 10, 20, 12, 0)]
    app.incognito.clock = lambda: now[0]

    await app.handle(say("/incognito"))
    now[0] += timedelta(minutes=29)
    assert (await app.handle(say("q1"))).startswith("🕶 ")  # each message pushes the timeout back
    now[0] += timedelta(minutes=29)
    assert await app.incognito.state("telegram:1") == "on"
    now[0] += timedelta(minutes=2)

    reply = await app.handle(say("q2"))

    assert reply == incognito.EXPIRED_NOTE + "y"
    assert [m["content"] for m in await history(db)] == ["q2", "y"]
    assert await app.incognito.state("telegram:1") == "off"


async def test_the_switch_survives_a_restart_but_the_conversation_does_not(app_and_model, db):
    app, _ = app_and_model
    await app.handle(say("/incognito"))
    app.incognito.buffer("telegram:1").append({"role": "user", "content": "q"})

    restarted = incognito.IncognitoSessions(db)

    assert await restarted.state("telegram:1") == "on"
    assert list(restarted.buffer("telegram:1")) == []


async def test_incognito_photo_reaches_the_model_but_is_not_saved(app_and_model, tmp_path):
    app, model = app_and_model
    model.responses = [Response(text="一只猫")]
    jpeg = b"\xff\xd8\xff\xe0fake-jpeg"

    reply = await app.handle(say("?? 这是什么", photos=[("AQADcat", jpeg)]))

    assert reply == "🕶 一只猫"
    user = model.calls[0]["messages"][-1]
    assert user["content"][0] == {"type": "text", "text": "这是什么"}
    assert user["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert not (tmp_path / "data" / "media").exists()


async def test_normal_photo_is_still_saved(app_and_model, tmp_path, db):
    app, model = app_and_model
    model.responses = [Response(text="午饭看起来不错")]

    await app.handle(say("午饭", photos=[("AQADlunch", b"\xff\xd8jpeg")]))

    saved = list((tmp_path / "data" / "media").glob("*/*_AQADlunch.jpg"))
    assert len(saved) == 1
    assert (await history(db))[0]["content"].startswith("午饭\n[照片已保存：data/media/")


async def test_logs_never_contain_what_was_said(app_and_model):
    from loguru import logger

    app, model = app_and_model
    model.responses = [tool_call_response("current_time", {}), Response(text=f"answer {SECRET}")]
    lines: list[str] = []
    handler = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    try:
        await app.handle(say(f"?? {SECRET}"))
    finally:
        logger.remove(handler)

    assert lines and not any(SECRET in line for line in lines)
    assert any("<incognito, " in line for line in lines)
    assert any("tool call: current_time (incognito)" in line for line in lines)
