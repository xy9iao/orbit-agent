from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from orbit.core.agent import Agent
from orbit.core.providers.base import Response
from orbit.core.router import ModelRouter
from orbit.core.tools import registry
from tests.conftest import FakeProvider, tool_call_response


def make_agent(config, db, memory, tmp_path: Path, responses):
    provider = FakeProvider("openai", responses)
    router = ModelRouter(config, {"openai": provider, "cheap": provider})
    persona = tmp_path / "persona"
    persona.mkdir()
    (persona / "system.md").write_text("You are Orbit test.", encoding="utf-8")
    return Agent(config.agent, router, registry, memory, db, persona), provider


@pytest.mark.asyncio
async def test_tool_loop_remembers_and_persists_history(config, db, memory, tmp_path):
    agent, provider = make_agent(
        config, db, memory, tmp_path,
        [tool_call_response("remember", {"note": "user likes tea"}), Response(text="记住了")],
    )
    reply = await agent.handle("cli", "local", "记住我喜欢喝茶")
    assert reply == "记住了"
    assert any("user likes tea" in e for e in memory.entries())

    # second call carries the tool result back to the model
    second = provider.calls[1]["messages"]
    assert second[-1]["role"] == "tool" and "remembered" in second[-1]["content"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["function"]["name"] == "remember"
    # system prompt includes persona + memory
    assert "You are Orbit test." in provider.calls[0]["messages"][0]["content"]

    hist = await db.get_history("cli:local", 10)
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert hist[1]["content"] == "记住了"


@pytest.mark.asyncio
async def test_set_reminder_tool_writes_db(config, db, memory, tmp_path):
    due = (datetime.now() + timedelta(hours=1)).replace(microsecond=0)
    agent, _ = make_agent(
        config, db, memory, tmp_path,
        [tool_call_response("set_reminder", {"due_at": due.isoformat(), "text": "喝水"}), Response(text="好")],
    )
    await agent.handle("telegram", "42", "一小时后提醒我喝水")
    rows = await db.list_reminders("pending")
    assert len(rows) == 1 and rows[0]["text"] == "喝水" and rows[0]["chat_id"] == "42"


@pytest.mark.asyncio
async def test_tool_round_limit_forces_final_answer(config, db, memory, tmp_path):
    # model keeps asking for tools; after max_tool_rounds the agent calls once more without tools
    loops = [tool_call_response("current_time", {}, f"c{i}") for i in range(config.agent.max_tool_rounds)]
    agent, provider = make_agent(config, db, memory, tmp_path, loops + [Response(text="final")])
    reply = await agent.handle("cli", "local", "loop")
    assert reply == "final"
    assert len(provider.calls) == config.agent.max_tool_rounds + 1
    assert provider.calls[-1]["tools"] is None
