"""The agent loop: persona + memory + history -> model -> tool calls -> reply."""

from __future__ import annotations

import base64
import mimetypes
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
    from orbit.packs import Pack
    from orbit.scheduler.scheduler import OrbitScheduler

PACK_RULES = """规则：
- 处理某个领域的事（排计划、记录、给建议）之前，先用 load_skill(pack) 读这个包的 SKILL.md，按里面的规则和记录格式做。
- 用户报告训练或饮食时，立刻用 add_record 记下（一个动作或一餐一条），然后只用一行确认记了什么。
- 用户纠正某个方法、或说出一个长期偏好时，用 write_skill 更新相关 section
  （写完整的新内容，保留仍然有效的规则）；锁定的 section 只能用户自己改。
- 查历史和进度用 query_records 和 record_stats，不要凭记忆编数字。
- 用户发来的照片已经保存，消息里有路径；记录和这张照片相关的内容时，把路径放进 data 的 photo 字段。"""


def image_part(path: Path) -> dict[str, Any]:
    """An OpenAI-format image content part with the file inlined as a data URL."""
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


class Agent:
    def __init__(
        self,
        config: AgentConfig,
        router: ModelRouter,
        registry: ToolRegistry,
        memory: MemoryStore,
        db: Database,
        persona_dir: Path,
        packs: dict[str, Pack] | None = None,
    ):
        self.config = config
        self.router = router
        self.registry = registry
        self.memory = memory
        self.db = db
        self.persona_dir = persona_dir
        self.packs: dict[str, Pack] = packs or {}
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

    def packs_prompt(self) -> str:
        if not self.packs:
            return ""
        listing = "\n".join(f"- {p.name}: {p.description}" for p in self.packs.values())
        return f"## 领域包（packs）\n{listing}\n\n{PACK_RULES}"

    def system_prompt(self, extra: str | None = None) -> str:
        parts = [self.persona(), f"## 当前时间\n{now_text()}", f"## 长期记忆\n{self.memory.read().strip()}"]
        if self.packs:
            parts.append(self.packs_prompt())
        if extra:
            parts.append(extra)
        return "\n\n".join(parts)

    @staticmethod
    def session_key(channel: str, chat_id: str) -> str:
        return f"{channel}:{chat_id}"

    # ---- conversation ----------------------------------------------------
    def _context(self, channel: str, chat_id: str) -> ToolContext:
        return ToolContext(
            channel=channel,
            chat_id=chat_id,
            db=self.db,
            memory=self.memory,
            scheduler=self.scheduler,
            router=self.router,
            packs=self.packs,
        )

    async def _loop(self, messages: list[dict[str, Any]], ctx: ToolContext) -> str:
        """Model <-> tools until the model answers without a tool call (or max_tool_rounds is hit)."""
        tools = self.registry.schemas() or None
        try:
            for _ in range(self.config.max_tool_rounds):
                resp = await self.router.complete("chat", messages, tools=tools)
                if not resp.tool_calls:
                    return resp.text
                messages.append(resp.assistant_message())
                for tc in resp.tool_calls:
                    logger.info("tool call: {}({})", tc.name, tc.arguments)
                    result = await self.registry.call(tc.name, tc.arguments, ctx)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            resp = await self.router.complete("chat", messages)  # no tools: force a final answer
            return resp.text
        except Exception as e:
            logger.exception("agent error")
            return f"出错了：{type(e).__name__}: {e}"

    async def handle(self, channel: str, chat_id: str, text: str, images: list[Path] | None = None) -> str:
        """One user turn. `images` (saved photo files) go to the model as image blocks; history keeps text."""
        session = self.session_key(channel, chat_id)
        history = await self.db.get_history(session, self.config.history_turns)
        await self.db.add_message(session, "user", text)

        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt()}]
        messages += history
        if images:
            content: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
            content += [image_part(path) for path in images]
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": text})

        reply = (await self._loop(messages, self._context(channel, chat_id))).strip() or "（没有内容）"
        await self.db.add_message(session, "assistant", reply)
        return reply

    async def run_routine(self, pack: str, prompt: str, channel: str, chat_id: str) -> str:
        """A pack routine: the full tool loop with the pack's SKILL.md in the system prompt."""
        skill = self.packs[pack].skill_path.read_text(encoding="utf-8").strip()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt(f"## 当前任务使用的 skill：{pack}\n{skill}")},
            {"role": "user", "content": f"[定时任务 · {pack}] {prompt}"},
        ]
        return (await self._loop(messages, self._context(channel, chat_id))).strip() or "（没有内容）"

    # ---- one-shot generation for jobs -----------------------------------
    async def generate(self, task: str, prompt: str, skill: str | None = None) -> str:
        extra = self.skill(skill) if skill else None
        messages = [
            {"role": "system", "content": self.system_prompt(extra)},
            {"role": "user", "content": prompt},
        ]
        resp = await self.router.complete(task, messages)
        return resp.text.strip()
