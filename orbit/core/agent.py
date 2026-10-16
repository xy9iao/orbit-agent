"""The agent loop: persona + memory + history -> model -> tool calls -> reply."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from orbit.config import AgentConfig
from orbit.core.memory import MemoryStore
from orbit.core.router import ModelRouter
from orbit.core.tools.registry import ToolContext, ToolRegistry
from orbit.core.tools.time_ import now_text
from orbit.storage.db import Database

if TYPE_CHECKING:
    from orbit.scheduler.scheduler import OrbitScheduler


class Agent:
    def __init__(
        self,
        config: AgentConfig,
        router: ModelRouter,
        registry: ToolRegistry,
        memory: MemoryStore,
        db: Database,
        persona_dir: Path,
    ):
        self.config = config
        self.router = router
        self.registry = registry
        self.memory = memory
        self.db = db
        self.persona_dir = persona_dir
        self.scheduler: OrbitScheduler | None = None

    # ---- prompt assembly -------------------------------------------------
    def persona(self) -> str:
        path = self.persona_dir / "system.md"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return f"You are {self.config.name}, the user's personal AI assistant."

    def skill(self, name: str) -> str:
        path = self.persona_dir / "skills" / f"{name}.md"
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""

    def system_prompt(self, extra: str | None = None) -> str:
        parts = [self.persona(), f"## 当前时间\n{now_text()}", f"## 长期记忆\n{self.memory.read().strip()}"]
        if extra:
            parts.append(extra)
        return "\n\n".join(parts)

    @staticmethod
    def session_key(channel: str, chat_id: str) -> str:
        return f"{channel}:{chat_id}"

    # ---- conversation ----------------------------------------------------
    async def handle(self, channel: str, chat_id: str, text: str) -> str:
        session = self.session_key(channel, chat_id)
        history = await self.db.get_history(session, self.config.history_turns)
        await self.db.add_message(session, "user", text)

        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt()}]
        messages += history
        messages.append({"role": "user", "content": text})

        ctx = ToolContext(
            channel=channel,
            chat_id=chat_id,
            db=self.db,
            memory=self.memory,
            scheduler=self.scheduler,
            router=self.router,
        )
        tools = self.registry.schemas() or None
        reply = ""
        try:
            for _ in range(self.config.max_tool_rounds):
                resp = await self.router.complete("chat", messages, tools=tools)
                if not resp.tool_calls:
                    reply = resp.text
                    break
                messages.append(resp.assistant_message())
                for tc in resp.tool_calls:
                    logger.info("tool call: {}({})", tc.name, tc.arguments)
                    result = await self.registry.call(tc.name, tc.arguments, ctx)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            else:
                resp = await self.router.complete("chat", messages)  # no tools: force a final answer
                reply = resp.text
        except Exception as e:
            logger.exception("agent error")
            reply = f"出错了：{type(e).__name__}: {e}"

        reply = reply.strip() or "（没有内容）"
        await self.db.add_message(session, "assistant", reply)
        return reply

    # ---- one-shot generation for jobs -----------------------------------
    async def generate(self, task: str, prompt: str, skill: str | None = None) -> str:
        extra = self.skill(skill) if skill else None
        messages = [
            {"role": "system", "content": self.system_prompt(extra)},
            {"role": "user", "content": prompt},
        ]
        resp = await self.router.complete(task, messages)
        return resp.text.strip()
