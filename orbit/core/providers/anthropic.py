"""Anthropic Messages API provider (Claude), behind the same interface as the OpenAI one.

Orbit's call sites speak OpenAI chat format, so this adapter translates both ways:
- messages: `system` turns become the top-level `system` prompt; an assistant turn with `tool_calls`
  becomes `tool_use` blocks; each `tool` turn becomes a `tool_result` block in a user turn (consecutive
  results share one turn, as the API requires); user `image_url` parts (data: or https: URLs) become
  image blocks.
- assistant turns produced by this provider carry `provider_blocks` (thinking with its signature, text,
  tool_use, in order), which are sent back verbatim: the API requires the thinking blocks of a turn that
  called tools.
- tools: `{"type": "function", "function": {name, description, parameters}}` becomes
  `{name, description, input_schema}`. `tool_choice` is never sent, so Claude decides (auto). A request
  without tools cannot carry tool blocks, so then earlier tool calls and results are sent as plain text
  (the agent's final no-tools round after max_tool_rounds).
- `response_format` json_schema (the radar's extraction schema) becomes structured outputs:
  `output_config.format`, run through the SDK's `transform_schema`.
- params: `effort` goes into `output_config.effort`; `cache_system: true` puts a `cache_control`
  breakpoint on the system prompt (prompt caching); `max_tokens` defaults to DEFAULT_MAX_TOKENS; any
  other param is passed straight to `messages.create`.
- batches: `submit_batch` / `batch_status` / `batch_results` run the same requests through the Message
  Batches API (half price, asynchronous); used by `orbit radar reextract`.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import Any

from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient, transform_schema

from .base import EmbedResponse, Response, ToolCall

DEFAULT_MAX_TOKENS = 8192


def _text(content: Any) -> str:
    """OpenAI message content (a string, None, or a list of text parts) as plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content if isinstance(part, dict))


def _user_blocks(content: Any) -> list[dict[str, Any]]:
    """User content -> text and image blocks. OpenAI `image_url` parts (data: or https: URLs) become images."""
    if not isinstance(content, list):
        text = _text(content)
        return [{"type": "text", "text": text}] if text else []
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and part.get("text"):
            blocks.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:") and ";base64," in url:
                media_type, data = url[5:].split(";base64,", 1)
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
            elif url:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    return blocks


def _tool_input(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        value = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {"_raw": arguments}
    return value if isinstance(value, dict) else {"_raw": arguments}


def convert_messages(
    messages: list[dict[str, Any]], *, tool_blocks: bool = True
) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI-format messages -> (system prompt, Anthropic messages). Same-role neighbours are merged.

    With tool_blocks=False, tool calls and tool results become text blocks instead of tool_use/tool_result.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    def add(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            system_parts.append(_text(msg.get("content")))
        elif role == "user":
            add("user", _user_blocks(msg.get("content")))
        elif role == "assistant":
            if tool_blocks and msg.get("provider_blocks"):
                add("assistant", copy.deepcopy(msg["provider_blocks"]))
                continue
            blocks: list[dict[str, Any]] = []
            text = _text(msg.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                name, args = fn.get("name", ""), _tool_input(fn.get("arguments"))
                if tool_blocks:
                    blocks.append({"type": "tool_use", "id": tc["id"], "name": name, "input": args})
                else:
                    call = json.dumps(args, ensure_ascii=False)
                    blocks.append({"type": "text", "text": f"[called tool {name} with {call}]"})
            add("assistant", blocks)
        elif role == "tool":
            result = _text(msg.get("content"))
            if tool_blocks:
                add("user", [{"type": "tool_result", "tool_use_id": msg["tool_call_id"], "content": result}])
            else:
                add("user", [{"type": "text", "text": f"[tool result: {result}]"}])
        else:
            raise ValueError(f"unsupported message role {role!r}")
    return "\n\n".join(p for p in system_parts if p), out


def block_param(block: Any) -> dict[str, Any] | None:
    """A response content block as a request block, or None for types that are not sent back."""
    if block.type == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if block.type == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": _tool_input(block.input)}
    return None


def convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for tool in tools:
        fn = tool.get("function", tool)
        converted.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


def output_format(response_format: dict[str, Any]) -> dict[str, Any]:
    """OpenAI `response_format` -> Anthropic structured-output `format`. Only json_schema is supported."""
    if response_format.get("type") != "json_schema":
        raise ValueError(f"unsupported response_format type {response_format.get('type')!r}")
    schema = response_format["json_schema"]["schema"]
    return {"type": "json_schema", "schema": transform_schema(schema)}


def to_response(message: Any) -> Response:
    """A Messages API `Message` as Orbit's Response (text, tool calls, usage, blocks to send back)."""
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    blocks: list[dict[str, Any]] = []
    for block in message.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=_tool_input(block.input)))
        param = block_param(block)
        if param is not None:
            blocks.append(param)

    usage: dict[str, int] = {}
    if message.usage:
        u = message.usage
        cache_write = u.cache_creation_input_tokens or 0
        cache_read = u.cache_read_input_tokens or 0
        usage = {
            # Total input, so usage records stay comparable with OpenAI's prompt_tokens.
            "prompt_tokens": (u.input_tokens or 0) + cache_write + cache_read,
            "completion_tokens": u.output_tokens or 0,
            "cache_creation_input_tokens": cache_write,
            "cache_read_input_tokens": cache_read,
        }
    return Response(
        text="".join(texts),
        tool_calls=tool_calls,
        usage=usage,
        raw=message,
        provider_blocks=blocks if tool_calls else None,
    )


class AnthropicProvider:
    def __init__(
        self, name: str, api_key: str, base_url: str | None = None, proxy: str | None = None, client: Any = None
    ):
        self.name = name
        if client is None:
            http_client = DefaultAsyncHttpxClient(proxy=proxy, timeout=120) if proxy else None
            client = AsyncAnthropic(api_key=api_key, base_url=base_url, http_client=http_client)
        self._client = client

    def build_request(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """The keyword arguments for `messages.create` (separate from the call so tests can inspect it)."""
        params = dict(params)
        response_format = params.pop("response_format", None)
        effort = params.pop("effort", None)
        cache_system = bool(params.pop("cache_system", False))
        output_config: dict[str, Any] = dict(params.pop("output_config", None) or {})
        if response_format is not None:
            output_config["format"] = output_format(response_format)
        if effort is not None:
            output_config["effort"] = effort

        system, converted = convert_messages(messages, tool_blocks=bool(tools))
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": params.pop("max_tokens", DEFAULT_MAX_TOKENS),
            "messages": converted,
            **params,
        }
        if system:
            block: dict[str, Any] = {"type": "text", "text": system}
            if cache_system:
                block["cache_control"] = {"type": "ephemeral"}
            kwargs["system"] = [block]
        if tools:
            kwargs["tools"] = convert_tools(tools)
        if output_config:
            kwargs["output_config"] = output_config
        return kwargs

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **params: Any,
    ) -> Response:
        message = await self._client.messages.create(**self.build_request(model, messages, tools, **params))
        return to_response(message)

    # ---- Message Batches API: asynchronous, half price, results within 24 hours ------------------------
    async def submit_batch(
        self, model: str, requests: Sequence[tuple[str, list[dict[str, Any]]]], **params: Any
    ) -> str:
        """Submit (custom_id, messages) pairs as one message batch; returns the batch id.

        Each request gets the same params as `complete` (structured output, effort, prompt caching).
        custom_id must match ^[a-zA-Z0-9_-]{1,64}$ and be unique within the batch.
        """
        batch = await self._client.messages.batches.create(
            requests=[
                {"custom_id": custom_id, "params": self.build_request(model, messages, None, **params)}
                for custom_id, messages in requests
            ]
        )
        return batch.id

    async def batch_status(self, batch_id: str) -> str:
        """`in_progress`, `canceling` or `ended` (results are available once ended)."""
        return (await self._client.messages.batches.retrieve(batch_id)).processing_status

    async def batch_results(self, batch_id: str) -> dict[str, Response | str]:
        """custom_id -> Response for succeeded requests, or an error text for errored/canceled/expired ones."""
        out: dict[str, Response | str] = {}
        async for entry in await self._client.messages.batches.results(batch_id):
            result = entry.result
            if result.type == "succeeded":
                out[entry.custom_id] = to_response(result.message)
            elif result.type == "errored":
                error = getattr(result.error, "error", result.error)
                out[entry.custom_id] = f"errored: {getattr(error, 'message', error)}"
            else:
                out[entry.custom_id] = result.type
        return out

    async def embed(self, model: str, texts: list[str], **params: Any) -> EmbedResponse:
        raise NotImplementedError("Anthropic has no embeddings API; route the embed task to an OpenAI provider")
