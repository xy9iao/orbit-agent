"""Pack records: validated add/update, query, delete and per-group stats over the `records` table."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, cast, delete, select, update

from orbit.packs import Pack
from orbit.storage.db import Database
from orbit.storage.models import Record

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
TIME_GROUPS = ("day", "week", "month")


class RecordError(ValueError):
    """A problem the model can fix (bad schema data, unknown id, bad date); its text goes back to the model."""


def parse_time(value: str | None, *, field: str) -> datetime | None:
    """ISO date or datetime -> naive local datetime (Orbit stores local time). None/"" -> None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise RecordError(f"{field} must be ISO format like 2026-10-19 or 2026-10-19T18:30") from None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.replace(microsecond=0)


def validate(pack: Pack, collection: str, data: Any) -> None:
    """Raise RecordError listing every schema violation (path: message)."""
    try:
        validator = pack.validator(collection)
    except KeyError as e:
        raise RecordError(str(e.args[0])) from None
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        parts = [f"{'.'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in errors[:10]]
        raise RecordError("data does not match the schema: " + "; ".join(parts))


def _out(row: Record) -> dict[str, Any]:
    return {
        "id": row.id,
        "pack": row.pack,
        "collection": row.collection,
        "occurred_at": row.occurred_at.isoformat(timespec="minutes"),
        "data": row.data,
    }


async def add_record(
    db: Database, pack: Pack, collection: str, data: dict[str, Any], occurred_at: datetime | None = None
) -> dict[str, Any]:
    validate(pack, collection, data)
    now = datetime.now().replace(microsecond=0)
    row = Record(
        pack=pack.name,
        collection=collection,
        occurred_at=occurred_at or now,
        data=data,
        schema_version=pack.schema_version,
        created_at=now,
    )
    async with db.session() as s:
        s.add(row)
        await s.commit()
    return _out(row)


async def get_record(db: Database, record_id: int) -> Record | None:
    async with db.session() as s:
        return await s.get(Record, record_id)


async def update_record(
    db: Database,
    packs: dict[str, Pack],
    record_id: int,
    data: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """Merge `data`'s top-level keys into the record (a key set to null is removed), then re-validate."""
    row = await get_record(db, record_id)
    if row is None:
        raise RecordError(f"no record with id {record_id}")
    pack = packs.get(row.pack)
    if pack is None:
        raise RecordError(f"record {record_id} belongs to pack '{row.pack}', which is not loaded")
    values: dict[str, Any] = {}
    if data:
        merged = {**row.data, **data}
        merged = {k: v for k, v in merged.items() if v is not None}
        validate(pack, row.collection, merged)
        values.update(data=merged, schema_version=pack.schema_version)
    if occurred_at is not None:
        values["occurred_at"] = occurred_at
    if not values:
        raise RecordError("nothing to update: pass data and/or occurred_at")
    async with db.session() as s:
        await s.execute(update(Record).where(Record.id == record_id).values(**values))
        await s.commit()
        fresh = await s.get(Record, record_id, populate_existing=True)
    assert fresh is not None
    return _out(fresh)


async def delete_record(db: Database, record_id: int) -> bool:
    async with db.session() as s:
        result = await s.execute(delete(Record).where(Record.id == record_id))
        await s.commit()
    return result.rowcount > 0


async def query_records(
    db: Database,
    pack: str,
    collection: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    contains: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Newest first. `until` is exclusive; `contains` is a case-insensitive substring of the record's JSON."""
    stmt = select(Record).where(Record.pack == pack, Record.collection == collection)
    if since is not None:
        stmt = stmt.where(Record.occurred_at >= since)
    if until is not None:
        stmt = stmt.where(Record.occurred_at < until)
    if contains:
        stmt = stmt.where(cast(Record.data, String).ilike(f"%{contains}%"))
    stmt = stmt.order_by(Record.occurred_at.desc(), Record.id.desc()).limit(max(1, min(int(limit), MAX_LIMIT)))
    async with db.session() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return [_out(r) for r in rows]


# ---- stats -------------------------------------------------------------------------------------------


def values_at(data: Any, path: str) -> list[Any]:
    """Values at a dotted path; lists along the way are flattened ("sets.weight" -> every set's weight)."""
    current: list[Any] = [data]
    for key in path.split("."):
        nxt: list[Any] = []
        for item in current:
            items = item if isinstance(item, list) else [item]
            for it in items:
                if isinstance(it, dict) and key in it:
                    nxt.append(it[key])
        current = nxt
    out: list[Any] = []
    for value in current:
        out.extend(value if isinstance(value, list) else [value])
    return [v for v in out if v is not None]


def _time_group(dt: datetime, group_by: str) -> str:
    if group_by == "day":
        return dt.date().isoformat()
    if group_by == "week":  # ISO week, labelled by its Monday
        return (dt.date() - timedelta(days=dt.weekday())).isoformat()
    return dt.strftime("%Y-%m")


def _is_number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def compute_stats(
    rows: Iterable[tuple[datetime, dict[str, Any]]], field: str, group_by: str | None = None
) -> list[dict[str, Any]]:
    """count/sum/min/max/first/last of `field` per group; rows must be oldest first."""
    groups: dict[str, list[Any]] = {}
    for occurred_at, data in rows:
        values = values_at(data, field)
        if not values:
            continue
        if not group_by:
            keys = ["all"]
        elif group_by in TIME_GROUPS:
            keys = [_time_group(occurred_at, group_by)]
        else:
            keys = [str(k) for k in values_at(data, group_by)] or ["(none)"]
        for key in keys:
            groups.setdefault(key, []).extend(values)
    out = []
    for key, values in groups.items():
        numbers = [v for v in values if _is_number(v)]
        out.append(
            {
                "group": key,
                "count": len(values),
                "sum": round(sum(numbers), 4) if numbers else None,
                "min": min(numbers) if numbers else None,
                "max": max(numbers) if numbers else None,
                "first": values[0],
                "last": values[-1],
            }
        )
    return sorted(out, key=lambda g: g["group"])


async def record_stats(
    db: Database,
    pack: str,
    collection: str,
    field: str,
    *,
    group_by: str | None = None,
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    stmt = select(Record.occurred_at, Record.data).where(Record.pack == pack, Record.collection == collection)
    if since is not None:
        stmt = stmt.where(Record.occurred_at >= since)
    stmt = stmt.order_by(Record.occurred_at, Record.id)
    async with db.session() as s:
        rows = (await s.execute(stmt)).all()
    return compute_stats(((r.occurred_at, r.data) for r in rows), field, group_by)
