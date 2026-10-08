from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orbit.config import AgentConfig, Config, ProviderConfig, TaskConfig
from orbit.core.memory import MemoryStore
from orbit.core.providers.base import Response, ToolCall
from orbit.storage.db import Database


class FakeProvider:
    """Returns scripted responses in order; records what it was asked."""

    def __init__(self, name: str, responses: list[Response] | None = None):
        self.name = name
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    async def complete(self, model, messages, tools=None, **params):
        self.calls.append({"model": model, "messages": messages, "tools": tools, "params": params})
        if self.responses:
            return self.responses.pop(0)
        return Response(text="ok", usage={"prompt_tokens": 1, "completion_tokens": 1})


def make_config() -> Config:
    return Config(
        providers={"openai": ProviderConfig(base_url="http://x"), "cheap": ProviderConfig(base_url="http://y")},
        tasks={
            "chat": TaskConfig(provider="openai", model="big"),
            "fast": TaskConfig(provider="cheap", model="small", params={"temperature": 0}),
        },
        agent=AgentConfig(history_turns=10, max_tool_rounds=4),
    )


@pytest.fixture
def config() -> Config:
    return make_config()


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def memory(tmp_path: Path) -> MemoryStore:
    m = MemoryStore(tmp_path / "memory")
    m.ensure()
    return m


def tool_call_response(name: str, args: dict[str, Any], call_id: str = "call_1") -> Response:
    return Response(text="", tool_calls=[ToolCall(id=call_id, name=name, arguments=args)])
