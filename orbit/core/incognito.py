"""Incognito mode: questions that leave no trace (no history, memory, records, photos or log text).

`/incognito` turns it on for a chat, `/incognito off` turns it off, and 30 minutes without a message
turns it off by itself. A message starting with `??` (or `？？`) is incognito on its own. While it is on:

- the conversation lives only in this process (`buffer`), never in the messages table;
- the model gets read-only tools (INCOGNITO_TOOLS), enforced by the registry, not by the prompt;
- photos are passed to the model from memory and never written to disk;
- logs show only the length of what was said.

The on/off state (session -> expiry time, no content) is kept in kv so it survives a restart: after a
restart Orbit must not silently go back to recording a chat the user believes is incognito.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from orbit.storage.db import Database

COMMAND = "/incognito"
PREFIXES = ("??", "？？")
IDLE = timedelta(minutes=30)
KV_PREFIX = "incognito:"
REPLY_MARK = "🕶 "
# Tools that only read; anything that writes (memory, skills, records, reminders, radar goal) is left out.
INCOGNITO_TOOLS = frozenset(
    {"current_time", "search_postings", "query_records", "record_stats", "load_skill", "list_reminders"}
)
PROMPT = (
    "## 无痕模式\n"
    "现在是无痕模式：这段对话不保存、不进长期记忆，你只能用只读工具。不要说你会记住什么；"
    "用户想记录或保存东西时，告诉他先发 /incognito off。"
)
ON_TEXT = "🕶 无痕模式已开启：这段对话不保存、不记忆、不进日志原文。30 分钟没说话自动关闭，发 /incognito off 关闭。"
OFF_TEXT = "无痕模式已关闭，之后的对话正常记录。"
ALREADY_OFF_TEXT = "无痕模式本来就是关着的。"
EXPIRED_NOTE = "（无痕模式 30 分钟没用，已自动关闭；这条已正常记录。）\n"


def parse(text: str) -> tuple[str | None, str]:
    """("on" | "off" | "once" | None, the text to answer). Accepts `/incognito@BotName` too."""
    stripped = text.strip()
    words = stripped.split()
    if words and words[0].split("@", 1)[0].lower() == COMMAND:
        arg = words[1].lower() if len(words) > 1 else "on"
        return ("off" if arg in ("off", "stop", "0", "关", "关闭") else "on"), ""
    for prefix in PREFIXES:
        if stripped.startswith(prefix):
            return "once", stripped[len(prefix) :].strip()
    return None, text


class IncognitoSessions:
    """Which chats are incognito (kv, survives restarts) and their in-memory conversation buffers."""

    def __init__(
        self,
        db: Database | Callable[[], Database],
        *,
        history_turns: int = 20,
        idle: timedelta = IDLE,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self._db = db  # or a callable returning the app's current Database (tests swap app.db)
        self.history_turns = history_turns
        self.idle = idle
        self.clock = clock
        self._buffers: dict[str, deque[dict[str, Any]]] = {}

    @property
    def db(self) -> Database:
        return self._db() if callable(self._db) else self._db

    def _key(self, session: str) -> str:
        return f"{KV_PREFIX}{session}"

    async def enable(self, session: str) -> None:
        self._buffers[session] = deque(maxlen=self.history_turns)
        await self.touch(session)

    async def touch(self, session: str) -> None:
        until = (self.clock() + self.idle).replace(microsecond=0)
        await self.db.kv_set(self._key(session), {"until": until.isoformat()})

    async def disable(self, session: str) -> bool:
        """Turn it off and drop the buffer; True if it was on."""
        was_on = bool(await self.db.kv_get(self._key(session)))
        await self.db.kv_set(self._key(session), None)
        self._buffers.pop(session, None)
        return was_on

    async def state(self, session: str) -> str:
        """"on", "off", or "expired" (it was on and timed out: turned off now)."""
        value = await self.db.kv_get(self._key(session))
        if not value:
            return "off"
        try:
            until = datetime.fromisoformat(value["until"])
        except (KeyError, TypeError, ValueError):
            until = None
        if until is not None and self.clock() < until:
            return "on"
        await self.disable(session)
        return "expired"

    def buffer(self, session: str) -> deque[dict[str, Any]]:
        """This chat's incognito turns (lost on restart, which is the point)."""
        return self._buffers.setdefault(session, deque(maxlen=self.history_turns))
