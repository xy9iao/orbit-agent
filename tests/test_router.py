from __future__ import annotations

import pytest

from orbit.core.router import ModelRouter
from tests.conftest import FakeProvider


@pytest.mark.asyncio
async def test_router_picks_model_by_task_and_records_usage(config):
    big, small = FakeProvider("openai"), FakeProvider("cheap")
    seen = []

    async def sink(task, provider, model, p, c):
        seen.append((task, provider, model, p, c))

    router = ModelRouter(config, {"openai": big, "cheap": small}, usage_sink=sink)

    await router.complete("fast", [{"role": "user", "content": "hi"}])
    assert small.calls[0]["model"] == "small"
    assert small.calls[0]["params"] == {"temperature": 0}

    await router.complete("unknown-task", [{"role": "user", "content": "hi"}])  # falls back to chat
    assert big.calls[0]["model"] == "big"

    assert seen == [("fast", "cheap", "small", 1, 1), ("unknown-task", "openai", "big", 1, 1)]


def test_router_errors_when_provider_missing(config):
    router = ModelRouter(config, {"openai": FakeProvider("openai")})
    with pytest.raises(RuntimeError):
        router.resolve("fast")
