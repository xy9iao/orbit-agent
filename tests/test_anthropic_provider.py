"""AnthropicProvider: OpenAI-format messages in, Messages API requests out, through a fake client (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import Message

from orbit.config import Config, ProviderConfig, Settings, TaskConfig
from orbit.core.agent import Agent
from orbit.core.providers import AnthropicProvider, OpenAIProvider, Response, ToolCall
from orbit.core.providers.anthropic import DEFAULT_MAX_TOKENS, convert_messages
from orbit.core.router import ModelRouter
from orbit.core.tools import registry
from orbit.radar.extract import SYSTEM_PROMPT, extract_posting, response_format
from tests.conftest import make_config

EXTRACTION = {
    "is_internship": True,
    "terms": ["Winter 2027"],
    "locations": ["Toronto, ON"],
    "remote_ok": False,
    "canada_eligible": True,
    "role_type": "ml_ai",
    "tech_stack": ["python", "pytorch"],
}


def message(content: list[dict[str, Any]], **usage: int) -> Message:
    """A real SDK Message, as `messages.create` would return it."""
    return Message.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "content": content,
            "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5, **usage},
        }
    )


class FakeMessages:
    def __init__(self, replies: list[Message]):
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        self.calls.append(json.loads(json.dumps(kwargs)))  # a snapshot: later mutation can't change it
        return self.replies.pop(0)


def fake_provider(*replies: Message) -> tuple[AnthropicProvider, FakeMessages]:
    messages = FakeMessages(list(replies))
    return AnthropicProvider("anthropic", api_key="unused", client=SimpleNamespace(messages=messages)), messages


THINKING = {"type": "thinking", "thinking": "Need the time first.", "signature": "sig-abc"}
TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "current_time", "input": {}}

# ---------------------------------------------------------------------------
# Message and tool conversion
# ---------------------------------------------------------------------------


def test_convert_messages_maps_system_tool_calls_and_merges_tool_results():
    system, out = convert_messages(
        [
            {"role": "system", "content": "You are Orbit."},
            {"role": "user", "content": "time and weather?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "current_time", "arguments": "{}"}},
                    {"id": "b", "function": {"name": "weather", "arguments": '{"city": "Waterloo"}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "09:00"},
            {"role": "tool", "tool_call_id": "b", "content": "sunny"},
        ]
    )
    assert system == "You are Orbit."
    assert out == [
        {"role": "user", "content": [{"type": "text", "text": "time and weather?"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "a", "name": "current_time", "input": {}},
                {"type": "tool_use", "id": "b", "name": "weather", "input": {"city": "Waterloo"}},
            ],
        },
        {
            "role": "user",  # both results in one user turn
            "content": [
                {"type": "tool_result", "tool_use_id": "a", "content": "09:00"},
                {"type": "tool_result", "tool_use_id": "b", "content": "sunny"},
            ],
        },
    ]


def test_convert_messages_without_tools_flattens_tool_history_to_text():
    _, out = convert_messages(
        [
            {"role": "user", "content": "time?"},
            {
                "role": "assistant",
                "content": "Checking.",
                "tool_calls": [{"id": "a", "function": {"name": "current_time", "arguments": "{}"}}],
                "provider_blocks": [THINKING, {"type": "text", "text": "Checking."}, TOOL_USE],
            },
            {"role": "tool", "tool_call_id": "a", "content": "09:00"},
        ],
        tool_blocks=False,
    )
    assert [b["type"] for turn in out for b in turn["content"]] == ["text", "text", "text", "text"]
    assert out[1]["content"][1]["text"] == "[called tool current_time with {}]"
    assert out[2]["content"] == [{"type": "text", "text": "[tool result: 09:00]"}]


def test_build_request_converts_tools_and_never_forces_tool_choice():
    provider, _ = fake_provider()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "remember",
                "description": "Save a note.",
                "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]},
            },
        }
    ]
    kwargs = provider.build_request("claude-sonnet-5-5", [{"role": "user", "content": "hi"}], tools, temperature=0)
    assert kwargs["tools"] == [
        {
            "name": "remember",
            "description": "Save a note.",
            "input_schema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]},
        }
    ]
    assert "tool_choice" not in kwargs
    assert kwargs["max_tokens"] == DEFAULT_MAX_TOKENS
    assert kwargs["temperature"] == 0  # unknown params pass straight through
    assert "system" not in kwargs and "output_config" not in kwargs


def test_build_request_structured_output_effort_and_cached_system_prompt():
    provider, _ = fake_provider()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "Title: ML Intern"}]
    kwargs = provider.build_request(
        "claude-haiku-5-5",
        messages,
        response_format=response_format(),
        effort="low",
        cache_system=True,
        max_tokens=2000,
    )
    assert kwargs["system"] == [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    fmt = kwargs["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert set(fmt["schema"]["required"]) == set(EXTRACTION)
    assert kwargs["output_config"]["effort"] == "low"
    assert kwargs["max_tokens"] == 2000
    for key in ("response_format", "effort", "cache_system"):
        assert key not in kwargs


def test_system_prompt_is_not_cached_unless_asked():
    provider, _ = fake_provider()
    kwargs = provider.build_request("m", [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
    assert kwargs["system"] == [{"type": "text", "text": "s"}]


def test_unsupported_response_format_is_rejected():
    provider, _ = fake_provider()
    with pytest.raises(ValueError, match="json_object"):
        provider.build_request("m", [{"role": "user", "content": "u"}], response_format={"type": "json_object"})


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


async def test_complete_maps_text_tool_calls_usage_and_keeps_blocks():
    provider, fake = fake_provider(
        message(
            [THINKING, {"type": "text", "text": "Let me check."}, TOOL_USE],
            cache_creation_input_tokens=100,
            cache_read_input_tokens=900,
        )
    )
    resp = await provider.complete("claude-sonnet-5-5", [{"role": "user", "content": "time?"}])
    assert resp.text == "Let me check."
    assert resp.tool_calls == [ToolCall(id="toolu_1", name="current_time", arguments={})]
    assert resp.usage == {
        "prompt_tokens": 1010,
        "completion_tokens": 5,
        "cache_creation_input_tokens": 100,
        "cache_read_input_tokens": 900,
    }
    assert resp.provider_blocks == [THINKING, {"type": "text", "text": "Let me check."}, TOOL_USE]
    assert fake.calls[0]["model"] == "claude-sonnet-5-5"


async def test_plain_reply_has_no_provider_blocks():
    provider, _ = fake_provider(message([THINKING, {"type": "text", "text": "Hi!"}]))
    resp = await provider.complete("m", [{"role": "user", "content": "hello"}])
    assert (resp.text, resp.tool_calls, resp.provider_blocks) == ("Hi!", [], None)
    assert "provider_blocks" not in resp.assistant_message()


async def test_tool_loop_sends_thinking_blocks_back_verbatim():
    provider, fake = fake_provider(message([THINKING, TOOL_USE]), message([{"type": "text", "text": "It is 09:00."}]))
    messages: list[dict[str, Any]] = [{"role": "user", "content": "time?"}]
    first = await provider.complete("m", messages, tools=registry.schemas())
    messages += [first.assistant_message(), {"role": "tool", "tool_call_id": "toolu_1", "content": "09:00"}]
    second = await provider.complete("m", messages, tools=registry.schemas())

    assert second.text == "It is 09:00."
    sent = fake.calls[1]["messages"]
    assert sent[1] == {"role": "assistant", "content": [THINKING, TOOL_USE]}
    result = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "09:00"}
    assert sent[2] == {"role": "user", "content": [result]}


async def test_embed_is_not_supported():
    provider, _ = fake_provider()
    with pytest.raises(NotImplementedError, match="embed"):
        await provider.embed("m", ["x"])


# ---------------------------------------------------------------------------
# Through the router: agent loop and radar extraction
# ---------------------------------------------------------------------------


def claude_config() -> Config:
    config = make_config()
    config.providers["anthropic"] = ProviderConfig(type="anthropic", api_key_env="ANTHROPIC_API_KEY")
    config.tasks["chat"] = TaskConfig(provider="anthropic", model="claude-sonnet-5-5")
    config.tasks["extract"] = TaskConfig(
        provider="anthropic", model="claude-haiku-5-5", params={"effort": "low", "cache_system": True}
    )
    return config


async def test_agent_tool_loop_on_claude(db, memory, tmp_path: Path):
    config = claude_config()
    provider, fake = fake_provider(
        message([THINKING, {"type": "tool_use", "id": "toolu_1", "name": "remember", "input": {"note": "likes tea"}}]),
        message([{"type": "text", "text": "Noted."}]),
    )
    router = ModelRouter(config, {"anthropic": provider})
    persona = tmp_path / "persona"
    persona.mkdir()
    (persona / "system.md").write_text("You are Orbit test.", encoding="utf-8")
    agent = Agent(config.agent, router, registry, memory, db, persona)

    assert await agent.handle("cli", "local", "remember that I like tea") == "Noted."
    assert any("likes tea" in e for e in memory.entries())
    first, second = fake.calls
    assert "You are Orbit test." in first["system"][0]["text"]
    assert "cache_control" not in first["system"][0]
    assert {t["name"] for t in first["tools"]} >= {"remember", "search_postings"}
    assert "tool_choice" not in first
    assert second["messages"][-2]["content"][0] == THINKING
    assert second["messages"][-1]["content"][0]["type"] == "tool_result"


async def test_radar_extraction_on_claude_uses_structured_output_and_cache():
    config = claude_config()
    provider, fake = fake_provider(message([{"type": "text", "text": json.dumps(EXTRACTION)}]))
    router = ModelRouter(config, {"anthropic": provider})
    posting = SimpleNamespace(
        title="ML Intern (Winter 2027)",
        location_raw="Toronto, ON",
        department=None,
        description_text="Train models with PyTorch. January to April 2027.",
    )
    outcome = await extract_posting(router, posting, company="Acme AI")

    assert (outcome.status, outcome.model) == ("ok", "claude-haiku-5-5")
    assert outcome.extraction["role_type"] == "ml_ai" and outcome.extraction["terms"] == ["Winter 2027"]
    [call] = fake.calls
    assert call["model"] == "claude-haiku-5-5"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["type"] == "json_schema"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_router_builds_anthropic_provider_from_settings(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = claude_config()
    settings = Settings(_env_file=None, openai_api_key="sk-o", anthropic_api_key="sk-a")
    with_key = ModelRouter.from_config(config, settings)
    assert isinstance(with_key.providers["anthropic"], AnthropicProvider)
    assert isinstance(with_key.providers["openai"], OpenAIProvider)

    without_key = ModelRouter.from_config(config, Settings(_env_file=None, openai_api_key="sk-o"))
    assert "anthropic" not in without_key.providers
    with pytest.raises(RuntimeError, match="missing API key"):
        without_key.resolve("chat")


def test_repo_config_routes_tasks_to_claude_and_keeps_embed_on_openai():
    from orbit.config import load_config

    config = load_config()
    assert config.providers["anthropic"].type == "anthropic"
    routes = {task: (tc.provider, tc.model) for task, tc in config.tasks.items()}
    assert routes["extract"] == ("anthropic", "claude-sonnet-5-5")
    assert config.tasks["extract"].params == {"effort": "low", "cache_system": True}
    for task in ("fast", "summarize"):
        assert routes[task] == ("anthropic", "claude-haiku-5-5")
    for task in ("chat", "briefing"):
        assert routes[task] == ("anthropic", "claude-opus-5-5")
    assert routes["code"] == ("anthropic", "claude-sonnet-5-5")
    assert routes["embed"] == ("openai", "text-embedding-3-small")


async def test_openai_provider_drops_provider_blocks():
    calls: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        msg = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)

    provider = OpenAIProvider(name="openai", base_url="http://127.0.0.1:9/v1", api_key="test-key")
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    turn = Response(text="", tool_calls=[ToolCall("a", "t", {})], provider_blocks=[THINKING]).assistant_message()
    await provider.complete("gpt-5", [{"role": "user", "content": "u"}, turn])
    assert "provider_blocks" not in calls[0]["messages"][1]
    assert "provider_blocks" in turn  # the caller's message is not mutated


# ---------------------------------------------------------------------------
# Message Batches
# ---------------------------------------------------------------------------


class FakeBatches:
    def __init__(self, entries: list[dict[str, Any]], status: str = "ended"):
        self.entries = entries
        self.status = status
        self.created: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.created.append(json.loads(json.dumps(kwargs)))
        return SimpleNamespace(id="msgbatch_1")

    async def retrieve(self, batch_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=batch_id, processing_status=self.status)

    async def results(self, batch_id: str):
        from anthropic.types.messages import MessageBatchIndividualResponse

        async def stream():
            for entry in self.entries:
                yield MessageBatchIndividualResponse.model_validate(entry)

        return stream()


def batch_provider(entries: list[dict[str, Any]], status: str = "ended") -> tuple[AnthropicProvider, FakeBatches]:
    batches = FakeBatches(entries, status)
    client = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    return AnthropicProvider("anthropic", api_key="unused", client=client), batches


async def test_submit_batch_sends_the_same_request_params_as_complete():
    provider, batches = batch_provider([])
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "Title: ML Intern"}]
    params = {"response_format": response_format(), "effort": "low", "cache_system": True}

    requests = [("posting-1", messages), ("posting-2", messages)]
    batch_id = await provider.submit_batch("claude-sonnet-5-5", requests, **params)

    assert batch_id == "msgbatch_1"
    requests = batches.created[0]["requests"]
    assert [r["custom_id"] for r in requests] == ["posting-1", "posting-2"]
    expected = json.loads(json.dumps(provider.build_request("claude-sonnet-5-5", messages, None, **params)))
    assert requests[0]["params"] == expected
    assert requests[0]["params"]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert requests[0]["params"]["output_config"]["effort"] == "low"
    assert requests[0]["params"]["output_config"]["format"]["type"] == "json_schema"
    assert "tools" not in requests[0]["params"] and "tool_choice" not in requests[0]["params"]


async def test_batch_status_and_results_map_succeeded_and_failed_requests():
    ok = message([{"type": "text", "text": json.dumps(EXTRACTION)}], cache_read_input_tokens=90)
    entries = [
        {"custom_id": "posting-1", "result": {"type": "succeeded", "message": ok.model_dump()}},
        {
            "custom_id": "posting-2",
            "result": {
                "type": "errored",
                "error": {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
            },
        },
        {"custom_id": "posting-3", "result": {"type": "expired"}},
    ]
    provider, _ = batch_provider(entries, status="in_progress")

    assert await provider.batch_status("msgbatch_1") == "in_progress"
    results = await provider.batch_results("msgbatch_1")

    assert isinstance(results["posting-1"], Response)
    assert json.loads(results["posting-1"].text) == EXTRACTION
    assert results["posting-1"].usage["prompt_tokens"] == 100
    assert results["posting-1"].usage["cache_read_input_tokens"] == 90
    assert results["posting-2"] == "errored: Overloaded"
    assert results["posting-3"] == "expired"


async def test_router_batch_results_records_usage_and_rejects_providers_without_batches():
    ok = message([{"type": "text", "text": "{}"}])
    entry = {"custom_id": "posting-1", "result": {"type": "succeeded", "message": ok.model_dump()}}
    provider, _ = batch_provider([entry])
    config = Config(
        providers={"anthropic": ProviderConfig(type="anthropic"), "openai": ProviderConfig(base_url="http://x")},
        tasks={
            "extract": TaskConfig(provider="anthropic", model="claude-sonnet-5-5"),
            "chat": TaskConfig(provider="openai", model="gpt"),
        },
    )
    usage: list[tuple] = []

    async def sink(*row):
        usage.append(row)

    openai = OpenAIProvider("openai", base_url="http://x", api_key="unused")
    router = ModelRouter(config, {"anthropic": provider, "openai": openai}, usage_sink=sink)

    assert await router.submit_batch("extract", [("posting-1", [{"role": "user", "content": "hi"}])]) == "msgbatch_1"
    assert await router.batch_status("extract", "msgbatch_1") == "ended"
    await router.batch_results("extract", "msgbatch_1")
    assert usage == [("extract", "anthropic", "claude-sonnet-5-5", 10, 5)]
    with pytest.raises(NotImplementedError, match="does not support batches"):
        await router.submit_batch("chat", [])


def test_describe_routes_lists_each_task_and_marks_missing_keys():
    config = Config(
        providers={"anthropic": ProviderConfig(type="anthropic"), "openai": ProviderConfig(base_url="http://x")},
        tasks={
            "chat": TaskConfig(provider="anthropic", model="claude-opus-5-5"),
            "embed": TaskConfig(provider="openai", model="text-embedding-3-small"),
        },
    )
    router = ModelRouter(config, {"anthropic": batch_provider([])[0]})

    assert router.describe_routes() == (
        "chat=anthropic:claude-opus-5-5, embed=openai:text-embedding-3-small (no key)"
    )
