from __future__ import annotations

from datetime import datetime

from .registry import ToolContext, tool


def _fmt(r: dict) -> str:
    return f"#{r['id']} {r['due_at'].replace('T', ' ')}: {r['text']}"


@tool
async def set_reminder(ctx: ToolContext, due_at: str, text: str) -> str:
    """Schedule a reminder that will be pushed to the user at a specific time.
    Call current_time first and convert relative times to an absolute local timestamp.

    Args:
        due_at: Absolute local time in ISO format, e.g. 2026-10-09T09:00:00
        text: What to remind the user about.
    """
    try:
        due = datetime.fromisoformat(due_at)
    except ValueError:
        return "error: due_at must be ISO format like 2026-10-09T09:00:00"
    if due.tzinfo is not None:
        due = due.astimezone().replace(tzinfo=None)
    if due <= datetime.now():
        return "error: that time is already in the past; call current_time and recompute"
    rid = await ctx.db.add_reminder(ctx.channel, ctx.chat_id, due, text)
    if ctx.scheduler is not None:
        ctx.scheduler.schedule_reminder(rid, due)
    return f"reminder #{rid} set for {due.isoformat(timespec='minutes')}: {text}"


@tool
async def list_reminders(ctx: ToolContext) -> str:
    """List all pending reminders with their ids and times."""
    rows = await ctx.db.list_reminders("pending")
    if not rows:
        return "no pending reminders"
    return "\n".join(_fmt(r) for r in rows)


@tool
async def cancel_reminder(ctx: ToolContext, reminder_id: int) -> str:
    """Cancel a pending reminder by id (see list_reminders).

    Args:
        reminder_id: The numeric id of the reminder.
    """
    ok = await ctx.db.set_reminder_status(reminder_id, "cancelled")
    if ok and ctx.scheduler is not None:
        ctx.scheduler.cancel_reminder(reminder_id)
    return f"reminder #{reminder_id} cancelled" if ok else f"no reminder #{reminder_id}"
