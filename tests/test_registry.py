from __future__ import annotations

import pytest

from orbit.core.tools.registry import ToolContext, ToolRegistry


def test_schema_from_signature_and_docstring():
    reg = ToolRegistry()

    @reg.tool
    def add_note(ctx: ToolContext, title: str, count: int = 1, tags: list[str] | None = None) -> str:
        """Add a note.

        Args:
            title: The note title.
            count: How many times.
        """
        return title * count

    (schema,) = reg.schemas()
    fn = schema["function"]
    assert fn["name"] == "add_note"
    assert fn["description"] == "Add a note."
    props = fn["parameters"]["properties"]
    assert "ctx" not in props
    assert props["title"] == {"type": "string", "description": "The note title."}
    assert props["count"] == {"type": "integer", "description": "How many times."}
    assert props["tags"]["type"] == "array"
    assert fn["parameters"]["required"] == ["title"]


@pytest.mark.asyncio
async def test_call_sync_async_and_errors(memory, db):
    reg = ToolRegistry()

    @reg.tool
    def sync_tool(x: int) -> dict:
        """Sync."""
        return {"double": x * 2}

    @reg.tool
    async def async_tool(ctx: ToolContext, x: int) -> str:
        """Async."""
        return f"{ctx.channel}:{x}"

    @reg.tool
    def boom() -> str:
        """Raises."""
        raise ValueError("nope")

    ctx = ToolContext(channel="cli", chat_id="local", db=db, memory=memory)
    assert await reg.call("sync_tool", {"x": 2}, ctx) == '{"double": 4}'
    assert await reg.call("async_tool", {"x": 3}, ctx) == "cli:3"
    assert (await reg.call("boom", {}, ctx)).startswith("error: ValueError")
    assert (await reg.call("missing", {}, ctx)).startswith("error: unknown tool")
