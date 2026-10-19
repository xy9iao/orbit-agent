"""Chat tools for domain packs: records (add / query / update / delete / stats) and skills (load / write)."""

from __future__ import annotations

from typing import Any

from orbit.packs import Pack, records, skills

from .registry import ToolContext, tool


def _pack(ctx: ToolContext, name: str) -> Pack:
    packs = ctx.packs or {}
    if name not in packs:
        known = ", ".join(packs) or "none loaded"
        raise records.RecordError(f"unknown pack '{name}' (packs: {known})")
    return packs[name]


def _err(e: Exception) -> str:
    return f"error: {e}"


@tool
async def add_record(
    ctx: ToolContext, pack: str, collection: str, data: dict, occurred_at: str | None = None
) -> Any:
    """Save one record (a workout exercise, a meal) to a pack collection. The data must match the
    collection's JSON Schema (load_skill shows the record format); on a mismatch the error says what to fix.

    Args:
        pack: Pack name, e.g. fitness or nutrition.
        collection: Collection in that pack, e.g. workouts or meals.
        data: The record, as an object matching the collection schema.
        occurred_at: When it happened, ISO local time (e.g. 2026-10-19T18:30); omit for now.
    """
    try:
        p = _pack(ctx, pack)
        when = records.parse_time(occurred_at, field="occurred_at")
        return await records.add_record(ctx.db, p, collection, data, when)
    except records.RecordError as e:
        return _err(e)


@tool
async def query_records(
    ctx: ToolContext,
    pack: str,
    collection: str,
    since: str | None = None,
    until: str | None = None,
    contains: str | None = None,
    limit: int = records.DEFAULT_LIMIT,
) -> Any:
    """List records of a pack collection, newest first (default 50, at most 200).

    Args:
        pack: Pack name.
        collection: Collection name.
        since: Only records at or after this ISO date/time.
        until: Only records before this ISO date/time.
        contains: Case-insensitive text that must appear in the record, e.g. an exercise name.
        limit: Maximum records to return (1-200).
    """
    try:
        _pack(ctx, pack)
        rows = await records.query_records(
            ctx.db,
            pack,
            collection,
            since=records.parse_time(since, field="since"),
            until=records.parse_time(until, field="until"),
            contains=contains,
            limit=limit,
        )
    except records.RecordError as e:
        return _err(e)
    return rows or "no records match"


@tool
async def update_record(
    ctx: ToolContext, record_id: int, data: dict | None = None, occurred_at: str | None = None
) -> Any:
    """Correct a saved record: top-level keys in data replace the stored ones (null removes a key); the result
    is validated again.

    Args:
        record_id: The record's id (from add_record or query_records).
        data: Fields to change, e.g. {"sets": [...]} or {"protein_g": 35}.
        occurred_at: New ISO local time, if the time was wrong.
    """
    try:
        when = records.parse_time(occurred_at, field="occurred_at")
        return await records.update_record(ctx.db, ctx.packs or {}, record_id, data, when)
    except records.RecordError as e:
        return _err(e)


@tool
async def delete_record(ctx: ToolContext, record_id: int) -> str:
    """Delete a saved record, e.g. a duplicate or one logged by mistake.

    Args:
        record_id: The record's id.
    """
    return f"deleted record {record_id}" if await records.delete_record(ctx.db, record_id) else "error: no such record"


@tool
async def record_stats(
    ctx: ToolContext,
    pack: str,
    collection: str,
    field: str,
    group_by: str | None = None,
    since: str | None = None,
) -> Any:
    """count / sum / min / max / first / last of one field, per group, oldest to newest within each group.

    Args:
        pack: Pack name.
        collection: Collection name.
        field: Dotted path into the record; lists are flattened, e.g. protein_g, kcal, sets.weight, sets.reps.
        group_by: day, week or month (by occurred_at), or a field path such as exercise or meal; omit for one total.
        since: Only records at or after this ISO date/time.
    """
    try:
        _pack(ctx, pack)
        stats = await records.record_stats(
            ctx.db, pack, collection, field, group_by=group_by, since=records.parse_time(since, field="since")
        )
    except records.RecordError as e:
        return _err(e)
    return stats or "no values for that field"


@tool
def load_skill(ctx: ToolContext, pack: str) -> str:
    """Read a pack's SKILL.md: its rules, methods, record format and preferences. Read it before planning,
    logging or advising in that domain.

    Args:
        pack: Pack name.
    """
    try:
        return skills.read_skill(_pack(ctx, pack))
    except records.RecordError as e:
        return _err(e)


@tool
def write_skill(ctx: ToolContext, pack: str, section: str, new_text: str) -> str:
    """Replace the body of one "## " section of a pack's SKILL.md (a new section name is appended). Use it
    when the user corrects a method or states a lasting preference. Locked sections are refused.

    Args:
        pack: Pack name.
        section: The section heading without "##", e.g. 训练方法.
        new_text: The complete new body of that section (keep every rule that still applies), no headings.
    """
    try:
        return skills.write_skill(_pack(ctx, pack), section, new_text)
    except records.RecordError as e:
        return _err(e)
