from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from orbit.core.providers import OpenAIProvider
from orbit.core.providers.base import EmbedResponse
from orbit.core.router import ModelRouter
from tests.conftest import FakeProvider, fake_embedding


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


async def test_router_embed_resolves_task_and_records_usage(config):
    big, small = FakeProvider("openai"), FakeProvider("cheap")
    seen = []

    async def sink(task, provider, model, p, c):
        seen.append((task, provider, model, p, c))

    router = ModelRouter(config, {"openai": big, "cheap": small}, usage_sink=sink)
    texts = ["ML intern, Toronto", "Backend co-op, Waterloo", "ML intern, Toronto"]

    vectors = await router.embed("embed", texts)

    assert small.embed_calls == [{"model": "embed-small", "texts": texts, "params": {}}]
    assert big.embed_calls == [] and big.calls == [] and small.calls == []
    assert len(vectors) == 3
    assert all(len(v) == 1536 for v in vectors)
    assert vectors == [fake_embedding(t) for t in texts]  # input order kept
    assert vectors[0] == vectors[2] and vectors[0] != vectors[1]
    assert math.isclose(sum(x * x for x in vectors[1]), 1.0)
    assert seen == [("embed", "cheap", "embed-small", 3, 0)]


async def test_router_embed_merges_params_and_falls_back_to_chat(config):
    big, small = FakeProvider("openai"), FakeProvider("cheap")
    router = ModelRouter(config, {"openai": big, "cheap": small})

    await router.embed("fast", ["a"], dimensions=256)
    assert small.embed_calls[0]["model"] == "small"
    assert small.embed_calls[0]["params"] == {"temperature": 0, "dimensions": 256}

    await router.embed("unknown-task", ["a"])
    assert big.embed_calls[0]["model"] == "big"


async def test_router_embed_empty_input_skips_provider(config):
    small = FakeProvider("cheap")
    seen = []

    async def sink(*args):
        seen.append(args)

    router = ModelRouter(config, {"openai": FakeProvider("openai"), "cheap": small}, usage_sink=sink)

    assert await router.embed("embed", []) == []
    assert small.embed_calls == []
    assert seen == []


async def test_router_embed_rejects_vector_count_mismatch(config):
    class ShortProvider(FakeProvider):
        async def embed(self, model, texts, **params):
            return EmbedResponse(vectors=[[1.0]], usage={})

    router = ModelRouter(config, {"openai": FakeProvider("openai"), "cheap": ShortProvider("cheap")})
    with pytest.raises(RuntimeError):
        await router.embed("embed", ["a", "b"])


class _StubEmbeddings:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


async def test_openai_provider_embed_sorts_by_index_and_maps_usage():
    result = SimpleNamespace(
        data=[
            SimpleNamespace(index=1, embedding=[0.0, 1.0]),
            SimpleNamespace(index=0, embedding=[1.0, 0.0]),
        ],
        usage=SimpleNamespace(prompt_tokens=7, total_tokens=7),
    )
    stub = _StubEmbeddings(result)
    provider = OpenAIProvider(name="openai", base_url="http://127.0.0.1:9/v1", api_key="test-key")
    provider._client = SimpleNamespace(embeddings=stub)

    resp = await provider.embed("text-embedding-3-small", ["first", "second"], dimensions=2)

    assert stub.calls == [{"model": "text-embedding-3-small", "input": ["first", "second"], "dimensions": 2}]
    assert resp.vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert resp.usage == {"prompt_tokens": 7, "completion_tokens": 0}
    assert resp.raw is result


async def test_openai_provider_embed_without_usage():
    result = SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[0.5])], usage=None)
    provider = OpenAIProvider(name="local", base_url="http://127.0.0.1:9/v1", api_key="test-key")
    provider._client = SimpleNamespace(embeddings=_StubEmbeddings(result))

    resp = await provider.embed("nomic", ["x"])

    assert resp.vectors == [[0.5]]
    assert resp.usage == {}
