"""Daily briefing: today's reminders + long-term memory -> short summary -> push."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from orbit.app import OrbitApp


async def build_briefing_prompt(orbit: OrbitApp) -> str:
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    reminders = await orbit.db.reminders_between(start, end)
    lines = [f"- {r['due_at'][11:16]} {r['text']}" for r in reminders] or ["（今天没有提醒）"]
    return (
        "请根据下面的信息生成今天的简报。\n\n"
        "## 今天的提醒\n" + "\n".join(lines) + "\n\n"
        "## 长期记忆\n" + (orbit.memory.read().strip() or "（空）")
    )


async def run_briefing(orbit: OrbitApp) -> str:
    prompt = await build_briefing_prompt(orbit)
    text = await orbit.agent.generate("briefing", prompt, skill="briefing")
    logger.info("briefing generated ({} chars)", len(text))
    await orbit.push(text)
    return text
