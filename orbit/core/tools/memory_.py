from __future__ import annotations

from .registry import ToolContext, tool


@tool
def remember(ctx: ToolContext, note: str) -> str:
    """Save a fact about the user to long-term memory (habits, projects, people, preferences, decisions). One concise sentence per call.

    Args:
        note: The fact to remember, written as a short standalone sentence.
    """
    return ctx.memory.remember(note)


@tool
def forget(ctx: ToolContext, keyword: str) -> str:
    """Delete long-term memory entries containing a keyword. Use when the user says something is no longer true.

    Args:
        keyword: Text that identifies the entries to remove.
    """
    n = ctx.memory.forget(keyword)
    return f"removed {n} entries" if n else "nothing matched"
