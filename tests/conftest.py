from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from orbit.config import AgentConfig, Config, ProviderConfig, TaskConfig
from orbit.core.memory import MemoryStore
from orbit.core.providers.base import Response, ToolCall
from orbit.storage.db import Database
from orbit.storage.migrate import upgrade_head
from orbit.storage.models import Base

ROOT = Path(__file__).resolve().parent.parent


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


def _test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or dotenv_values(ROOT / ".env").get("TEST_DATABASE_URL")
    if not url:
        pytest.fail(
            "TEST_DATABASE_URL is not set. Start Postgres (docker compose up -d postgres) and set it in the "
            "environment or .env, e.g. postgresql+asyncpg://orbit:orbit@127.0.0.1:5432/orbit_test",
            pytrace=False,
        )
    # Every test truncates all tables, so never point this at a real database.
    if "test" not in (make_url(url).database or ""):
        pytest.fail(f"refusing to run tests against a database without 'test' in its name: {url}", pytrace=False)
    return url


@pytest.fixture(scope="session")
def database_url() -> str:
    """Migrate the test database to head once per test session."""
    url = _test_database_url()

    async def _migrate() -> None:
        engine = create_async_engine(url)
        try:
            await upgrade_head(engine)
        finally:
            await engine.dispose()

    asyncio.run(_migrate())
    return url


@pytest.fixture
async def db(database_url: str):
    database = Database(database_url)
    await database.connect()
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with database.engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield database
    await database.close()


@pytest.fixture
def memory(tmp_path: Path) -> MemoryStore:
    m = MemoryStore(tmp_path / "memory")
    m.ensure()
    return m


def tool_call_response(name: str, args: dict[str, Any], call_id: str = "call_1") -> Response:
    return Response(text="", tool_calls=[ToolCall(id=call_id, name=name, arguments=args)])
