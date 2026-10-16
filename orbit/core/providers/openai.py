"""OpenAI-compatible chat completions and embeddings provider (OpenAI, DeepSeek, Ollama, vLLM...)."""

from __future__ import annotations

import json
from typing import Any

import httpx
from openai import AsyncOpenAI

from .base import EmbedResponse, Response, ToolCall


class OpenAIProvider:
    def __init__(self, name: str, base_url: str, api_key: str, proxy: str | None = None):
        self.name = name
        http_client = httpx.AsyncClient(proxy=proxy, timeout=120) if proxy else None
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, http_client=http_client)

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **params: Any,
    ) -> Response:
        kwargs: dict[str, Any] = {"model": model, "messages": messages, **params}
        if tools:
            kwargs["tools"] = tools
        completion = await self._client.chat.completions.create(**kwargs)
        msg = completion.choices[0].message

        tool_calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        usage: dict[str, int] = {}
        if completion.usage:
            usage = {
                "prompt_tokens": completion.usage.prompt_tokens or 0,
                "completion_tokens": completion.usage.completion_tokens or 0,
            }
        return Response(text=msg.content or "", tool_calls=tool_calls, usage=usage, raw=completion)

    async def embed(self, model: str, texts: list[str], **params: Any) -> EmbedResponse:
        result = await self._client.embeddings.create(model=model, input=texts, **params)
        # Each item carries its input `index`; sort so vectors always line up with `texts`.
        items = sorted(result.data, key=lambda item: item.index)
        usage: dict[str, int] = {}
        if result.usage:
            usage = {"prompt_tokens": result.usage.prompt_tokens or 0, "completion_tokens": 0}
        return EmbedResponse(vectors=[list(item.embedding) for item in items], usage=usage, raw=result)
