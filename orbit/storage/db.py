"""Postgres storage (async SQLAlchemy): conversation history, reminders, model usage, key-value state.

The public methods keep the shapes Orbit has always used: plain dicts, with
timestamps as naive local ISO strings (seconds precision).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import KV, Message, Reminder, Usage


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _naive(dt: datetime) -> datetime:
    """Columns are `timestamp without time zone` in local time."""
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.replace(microsecond=0)


def _row(m: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v.isoformat(timespec="seconds") if isinstance(v, datetime) else v for k, v in m.items()}


class Database:
    def __init__(self, url: str):
        self.url = url
        self._engine: AsyncEngine | None = None
        self._sessions: async_sessionmaker[AsyncSession] | None = None

    async def connect(self) -> None:
        self._engine = create_async_engine(self.url, pool_pre_ping=True, pool_size=5, max_overflow=5)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessions = None

    @property
    def engine(self) -> AsyncEngine:
        assert self._engine is not None, "Database not connected"
        return self._engine

    def session(self) -> AsyncSession:
        """ORM session for the radar code; callers commit explicitly."""
        assert self._sessions is not None, "Database not connected"
        return self._sessions()

    async def migrate(self) -> None:
        """Bring the schema to the latest Alembic revision (idempotent)."""
        from .migrate import upgrade_head

        await upgrade_head(self.engine)

    async def ping(self) -> None:
        """Raises if the database is unreachable."""
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    # ---- messages -------------------------------------------------------
    async def add_message(self, session: str, role: str, content: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                insert(Message).values(session=session, role=role, content=content, created_at=_now())
            )

    async def get_history(self, session: str, limit: int) -> list[dict[str, str]]:
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(Message.role, Message.content)
                    .where(Message.session == session)
                    .order_by(Message.id.desc())
                    .limit(limit)
                )
            ).all()
        return [{"role": r.role, "content": r.content} for r in reversed(rows)]

    async def clear_history(self, session: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(delete(Message).where(Message.session == session))

    # ---- reminders ------------------------------------------------------
    async def add_reminder(self, channel: str, chat_id: str, due_at: datetime, text: str) -> int:
        async with self.engine.begin() as conn:
            rid = await conn.scalar(
                insert(Reminder)
                .values(
                    channel=channel,
                    chat_id=chat_id,
                    due_at=_naive(due_at),
                    text=text,
                    status="pending",
                    created_at=_now(),
                )
                .returning(Reminder.id)
            )
        return int(rid or 0)

    async def get_reminder(self, reminder_id: int) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(select(Reminder.__table__).where(Reminder.id == reminder_id))
            ).mappings().first()
        return _row(row) if row else None

    async def list_reminders(self, status: str = "pending") -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(Reminder.__table__)
                    .where(Reminder.status == status)
                    .order_by(Reminder.due_at, Reminder.id)
                )
            ).mappings().all()
        return [_row(r) for r in rows]

    async def reminders_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(Reminder.__table__)
                    .where(
                        Reminder.due_at >= _naive(start),
                        Reminder.due_at < _naive(end),
                        Reminder.status == "pending",
                    )
                    .order_by(Reminder.due_at, Reminder.id)
                )
            ).mappings().all()
        return [_row(r) for r in rows]

    async def set_reminder_status(self, reminder_id: int, status: str) -> bool:
        async with self.engine.begin() as conn:
            result = await conn.execute(update(Reminder).where(Reminder.id == reminder_id).values(status=status))
        return result.rowcount > 0

    # ---- usage ----------------------------------------------------------
    async def add_usage(
        self, task: str, provider: str, model: str, prompt_tokens: int, completion_tokens: int
    ) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                insert(Usage).values(
                    task=task,
                    provider=provider,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    created_at=_now(),
                )
            )

    async def usage_since(self, since: datetime) -> list[dict[str, Any]]:
        stmt = (
            select(
                Usage.model,
                func.count().label("calls"),
                func.sum(Usage.prompt_tokens).label("prompt_tokens"),
                func.sum(Usage.completion_tokens).label("completion_tokens"),
            )
            .where(Usage.created_at >= _naive(since))
            .group_by(Usage.model)
            .order_by(Usage.model)
        )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [dict(r) for r in rows]

    async def usage_today(self) -> list[dict[str, Any]]:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return await self.usage_since(start)

    # ---- kv -------------------------------------------------------------
    async def kv_get(self, key: str, default: Any = None) -> Any:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(KV.value).where(KV.key == key))).first()
        return row.value if row else default

    async def kv_set(self, key: str, value: Any) -> None:
        stmt = pg_insert(KV).values(key=key, value=value)
        stmt = stmt.on_conflict_do_update(index_elements=[KV.key], set_={"value": stmt.excluded.value})
        async with self.engine.begin() as conn:
            await conn.execute(stmt)
