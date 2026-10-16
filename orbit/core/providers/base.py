"""Provider protocol: every model backend returns the same Response shape."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Response:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None

    def assistant_message(self) -> dict[str, Any]:
        """Re-encode as an OpenAI-style assistant message so it can be fed back in the tool loop."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in self.tool_calls
            ]
        return msg


@dataclass
class EmbedResponse:
    """One vector per input text, in input order."""

    vectors: list[list[float]]
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


class Provider(Protocol):
    name: str

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **params: Any,
    ) -> Response: ...

    async def embed(self, model: str, texts: list[str], **params: Any) -> EmbedResponse: ...
