from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from orbit.storage.db import Database
from orbit.storage.models import Message

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("migrate_sqlite_to_pg", ROOT / "scripts" / "migrate_sqlite_to_pg.py")
assert _spec and _spec.loader
migrate_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate_script)

# The schema the aiosqlite Database created (main @ 7067f98).
OLD_SCHEMA = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, chat_id TEXT NOT NULL, due_at TEXT NOT NULL,
    text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
);
CREATE TABLE usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def make_old_sqlite(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.executemany(
        "INSERT INTO messages(id, session, role, content, created_at) VALUES (?,?,?,?,?)",
        [
            (1, "telegram:42", "user", "你好", "2026-09-01T08:00:00"),
            (2, "telegram:42", "assistant", "hi there", "2026-09-01T08:00:05"),
            (5, "cli:local", "user", "after a gap", "2026-09-02T10:00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO reminders(id, channel, chat_id, due_at, text, status, created_at) VALUES (?,?,?,?,?,?,?)",
        [
            (3, "telegram", "42", "2026-09-03T09:30:00", "dentist", "done", "2026-09-01T08:01:00"),
            (7, "telegram", "42", "2030-01-02T09:30:15", "renew passport", "pending", "2026-09-02T11:00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO usage(id, task, provider, model, prompt_tokens, completion_tokens, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (1, "chat", "openai", "big", 100, 20, "2026-09-01T08:00:05"),
            (2, "fast", "openai", "small", 7, 3, "2026-09-01T08:00:06"),
        ],
    )
    conn.executemany(
        "INSERT INTO kv(key, value) VALUES (?,?)",
        [
            ("seen", json.dumps({"ids": [1, 2], "note": "é"}, ensure_ascii=False)),
            ("count", json.dumps(3)),
        ],
    )
    conn.commit()
    conn.close()


async def test_copies_once_and_sequences_continue(db: Database, tmp_path: Path):
    sqlite_path = tmp_path / "orbit.db"
    make_old_sqlite(sqlite_path)

    first = await migrate_script.migrate_sqlite(sqlite_path, db.url)
    assert first == {"messages": (3, 0), "reminders": (2, 0), "usage": (2, 0), "kv": (2, 0)}
    second = await migrate_script.migrate_sqlite(sqlite_path, db.url)
    assert second == {"messages": (0, 3), "reminders": (0, 2), "usage": (0, 2), "kv": (0, 2)}

    assert await db.get_history("telegram:42", 10) == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "hi there"},
    ]
    assert await db.get_history("cli:local", 10) == [{"role": "user", "content": "after a gap"}]

    reminder = await db.get_reminder(7)
    assert reminder == {
        "id": 7,
        "channel": "telegram",
        "chat_id": "42",
        "due_at": "2030-01-02T09:30:15",
        "text": "renew passport",
        "status": "pending",
        "created_at": "2026-09-02T11:00:00",
    }
    assert [r["id"] for r in await db.list_reminders("done")] == [3]

    usage = {r["model"]: r for r in await db.usage_since(datetime(2026, 9, 1))}
    assert usage["big"] == {"model": "big", "calls": 1, "prompt_tokens": 100, "completion_tokens": 20}
    assert usage["small"]["calls"] == 1

    assert await db.kv_get("seen") == {"ids": [1, 2], "note": "é"}
    assert await db.kv_get("count") == 3

    # New rows get fresh ids past the copied ones instead of colliding with them.
    assert await db.add_reminder("cli", "1", datetime(2031, 1, 1), "new") == 8
    await db.add_message("cli:local", "assistant", "new reply")
    async with db.engine.connect() as conn:
        ids = (await conn.scalars(select(Message.id).order_by(Message.id))).all()
    assert ids == [1, 2, 5, 6]
