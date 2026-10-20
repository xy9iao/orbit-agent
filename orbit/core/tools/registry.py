"""Tool registry: a decorator turns a plain Python function into a model-callable tool.

Schema is generated from the signature + docstring, so adding a tool is just writing a function.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import types
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from orbit.core.memory import MemoryStore
    from orbit.core.router import ModelRouter
    from orbit.packs import Pack
    from orbit.scheduler.scheduler import OrbitScheduler
    from orbit.storage.db import Database


@dataclass
class ToolContext:
    """What a tool gets to know about the conversation it is called from."""

    channel: str
    chat_id: str
    db: Database
    memory: MemoryStore
    scheduler: OrbitScheduler | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    router: ModelRouter | None = None  # for tools that need the model (e.g. embeddings); None in bare contexts
    packs: dict[str, Pack] | None = None  # loaded domain packs (orbit.packs), by name


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable[..., Any]
    parameters: dict[str, Any]
    wants_ctx: bool

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


_TYPE_MAP: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _json_type(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    origin = typing.get_origin(annotation)
    if origin in (types.UnionType, typing.Union):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _json_type(args[0]) if args else {"type": "string"}
    if origin is list:
        inner = typing.get_args(annotation)
        return {"type": "array", "items": _json_type(inner[0]) if inner else {"type": "string"}}
    if origin is dict:
        return {"type": "object"}
    return {"type": _TYPE_MAP.get(annotation, "string")}


def _parse_docstring(doc: str | None) -> tuple[str, dict[str, str]]:
    """First paragraph = description; 'name: text' lines under 'Args:' = param docs.

    A line indented deeper than the param lines continues the previous param's text (Google style), so a
    long description can wrap, and an indented 'key: text' line inside it is not mistaken for a param.
    """
    if not doc:
        return "", {}
    raw_lines = inspect.cleandoc(doc).splitlines()
    desc_lines: list[str] = []
    params: dict[str, str] = {}
    in_args = False
    desc_done = False
    arg_indent: int | None = None
    current: str | None = None
    for raw in raw_lines:
        ln = raw.strip()
        if ln.lower().rstrip(":") in ("args", "arguments", "params", "parameters"):
            in_args = True
            continue
        if in_args:
            if not ln:
                continue
            indent = len(raw) - len(raw.lstrip())
            if current is not None and arg_indent is not None and indent > arg_indent:
                params[current] += " " + ln
                continue
            m = re.match(r"^(\w+)\s*(?:\([^)]*\))?\s*:\s*(.+)$", ln)
            if m:
                arg_indent, current = indent, m.group(1)
                params[current] = m.group(2)
            continue
        if ln == "" and desc_lines:
            desc_done = True  # first paragraph only
        elif ln and not desc_done:
            desc_lines.append(ln)
    return " ".join(desc_lines), params


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, fn: Callable[..., Any], name: str | None = None) -> Tool:
        sig = inspect.signature(fn)
        hints = typing.get_type_hints(fn)
        description, param_docs = _parse_docstring(fn.__doc__)
        props: dict[str, Any] = {}
        required: list[str] = []
        wants_ctx = False
        for pname, param in sig.parameters.items():
            if pname == "ctx":
                wants_ctx = True
                continue
            schema = _json_type(hints.get(pname, param.annotation))
            if pname in param_docs:
                schema["description"] = param_docs[pname]
            props[pname] = schema
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        tool = Tool(
            name=name or fn.__name__,
            description=description,
            fn=fn,
            parameters={"type": "object", "properties": props, "required": required},
            wants_ctx=wants_ctx,
        )
        self._tools[tool.name] = tool
        return tool

    def tool(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Decorator form: @tool above a function registers it."""
        self.register(fn)
        return fn

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self, allow: frozenset[str] | set[str] | None = None) -> list[dict[str, Any]]:
        """Every tool's schema, or only the ones named in `allow`."""
        return [t.schema() for t in self._tools.values() if allow is None or t.name in allow]

    async def call(
        self, name: str, arguments: dict[str, Any], ctx: ToolContext, allow: frozenset[str] | set[str] | None = None
    ) -> str:
        """Run a tool. With `allow`, any other name is refused even if the model calls it anyway."""
        if allow is not None and name not in allow:
            return f"error: tool '{name}' is not available right now"
        tool = self._tools.get(name)
        if tool is None:
            return f"error: unknown tool '{name}'"
        kwargs = dict(arguments)
        if tool.wants_ctx:
            kwargs["ctx"] = ctx
        try:
            result = tool.fn(**kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as e:  # tools must never crash the agent loop
            logger.exception("tool {} failed", name)
            return f"error: {type(e).__name__}: {e}"
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)


registry = ToolRegistry()
tool = registry.tool

__all__ = ["ToolContext", "ToolRegistry", "Tool", "registry", "tool", "Awaitable", "asyncio"]
