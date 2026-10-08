"""SQLite storage: conversation history, reminders, model usage, key-value state."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session, id);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    # ---- messages -------------------------------------------------------
    async def add_message(self, session: str, role: str, content: str) -> None:
        await self.conn.execute(
            "INSERT INTO messages(session, role, content, created_at) VALUES (?,?,?,?)",
            (session, role, content, _now()),
        )
        await self.conn.commit()

    async def get_history(self, session: str, limit: int) -> list[dict[str, str]]:
        cur = await self.conn.execute(
            "SELECT role, content FROM messages WHERE session=? ORDER BY id DESC LIMIT ?",
            (session, limit),
        )
        rows = await cur.fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def clear_history(self, session: str) -> None:
        await self.conn.execute("DELETE FROM messages WHERE session=?", (session,))
        await self.conn.commit()

    # ---- reminders ------------------------------------------------------
    async def add_reminder(self, channel: str, chat_id: str, due_at: datetime, text: str) -> int:
        cur = await self.conn.execute(
            "INSERT INTO reminders(channel, chat_id, due_at, text, status, created_at) "
            "VALUES (?,?,?,?,'pending',?)",
            (channel, chat_id, due_at.isoformat(timespec="seconds"), text, _now()),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def get_reminder(self, reminder_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_reminders(self, status: str = "pending") -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM reminders WHERE status=? ORDER BY due_at", (status,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def reminders_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM reminders WHERE due_at>=? AND due_at<? AND status='pending' ORDER BY due_at",
            (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def set_reminder_status(self, reminder_id: int, status: str) -> bool:
        cur = await self.conn.execute(
            "UPDATE reminders SET status=? WHERE id=?", (status, reminder_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    # ---- usage ----------------------------------------------------------
    async def add_usage(
        self, task: str, provider: str, model: str, prompt_tokens: int, completion_tokens: int
    ) -> None:
        await self.conn.execute(
            "INSERT INTO usage(task, provider, model, prompt_tokens, completion_tokens, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (task, provider, model, prompt_tokens, completion_tokens, _now()),
        )
        await self.conn.commit()

    async def usage_since(self, since: datetime) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT model, COUNT(*) AS calls, SUM(prompt_tokens) AS prompt_tokens, "
            "SUM(completion_tokens) AS completion_tokens FROM usage WHERE created_at>=? GROUP BY model",
            (since.isoformat(timespec="seconds"),),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def usage_today(self) -> list[dict[str, Any]]:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return await self.usage_since(start)

    # ---- kv -------------------------------------------------------------
    async def kv_get(self, key: str, default: Any = None) -> Any:
        cur = await self.conn.execute("SELECT value FROM kv WHERE key=?", (key,))
        row = await cur.fetchone()
        return json.loads(row["value"]) if row else default

    async def kv_set(self, key: str, value: Any) -> None:
        await self.conn.execute(
            "INSERT INTO kv(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        await self.conn.commit()
