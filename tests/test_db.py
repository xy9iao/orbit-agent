from __future__ import annotations

from datetime import UTC, datetime, timedelta

from orbit.storage.db import Database


async def test_reminder_timestamps_round_trip(db: Database):
    due = datetime(2030, 1, 2, 9, 30, 15, 987654)
    rid = await db.add_reminder("telegram", "42", due, "dentist")

    row = await db.get_reminder(rid)
    assert row is not None
    # Same shape callers have always parsed: naive local ISO string, seconds precision.
    assert row["due_at"] == "2030-01-02T09:30:15"
    assert datetime.fromisoformat(row["due_at"]) == due.replace(microsecond=0)
    assert row["due_at"].replace("T", " ")[11:16] == "09:30"
    assert isinstance(row["created_at"], str) and len(row["created_at"]) == 19
    assert row["status"] == "pending" and row["channel"] == "telegram" and row["chat_id"] == "42"


async def test_aware_due_at_is_stored_as_local_time(db: Database):
    due = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)
    rid = await db.add_reminder("cli", "1", due, "x")
    row = await db.get_reminder(rid)
    assert row["due_at"] == due.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")


async def test_reminder_listing_status_and_window(db: Database):
    now = datetime.now().replace(microsecond=0)
    later = await db.add_reminder("cli", "1", now + timedelta(hours=2), "later")
    sooner = await db.add_reminder("cli", "1", now + timedelta(hours=1), "sooner")
    tomorrow = await db.add_reminder("cli", "1", now + timedelta(days=2), "far")

    assert [r["id"] for r in await db.list_reminders("pending")] == [sooner, later, tomorrow]
    window = await db.reminders_between(now, now + timedelta(hours=3))
    assert [r["text"] for r in window] == ["sooner", "later"]

    assert await db.set_reminder_status(sooner, "done") is True
    assert await db.set_reminder_status(999_999, "done") is False
    assert [r["id"] for r in await db.list_reminders("done")] == [sooner]
    assert [r["text"] for r in await db.reminders_between(now, now + timedelta(hours=3))] == ["later"]


async def test_usage_today_aggregates_per_model(db: Database):
    await db.add_usage("chat", "openai", "big", 100, 20)
    await db.add_usage("chat", "openai", "big", 50, 5)
    await db.add_usage("fast", "cheap", "small", 7, 3)

    rows = {r["model"]: r for r in await db.usage_today()}
    assert rows["big"] == {"model": "big", "calls": 2, "prompt_tokens": 150, "completion_tokens": 25}
    assert rows["small"]["calls"] == 1
    assert await db.usage_since(datetime.now() + timedelta(days=1)) == []


async def test_kv_set_upserts_json(db: Database):
    assert await db.kv_get("seen", default=[]) == []
    await db.kv_set("seen", ["a", "é"])
    await db.kv_set("seen", {"ids": [1, 2], "ok": True})
    assert await db.kv_get("seen") == {"ids": [1, 2], "ok": True}
    await db.kv_set("count", 3)
    assert await db.kv_get("count") == 3


async def test_history_order_limit_and_clear(db: Database):
    for i in range(5):
        await db.add_message("cli:local", "user" if i % 2 == 0 else "assistant", f"m{i}")
    await db.add_message("telegram:1", "user", "other session")

    history = await db.get_history("cli:local", 3)
    assert [m["content"] for m in history] == ["m2", "m3", "m4"]
    assert history[0] == {"role": "user", "content": "m2"}

    await db.clear_history("cli:local")
    assert await db.get_history("cli:local", 10) == []
    assert await db.get_history("telegram:1", 10) == [{"role": "user", "content": "other session"}]


async def test_ping_and_fresh_state_per_test(db: Database):
    await db.ping()
    # The fixture truncates with RESTART IDENTITY, so ids start at 1 in every test.
    assert await db.add_reminder("cli", "1", datetime(2031, 1, 1), "first") == 1
