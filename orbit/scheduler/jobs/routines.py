"""Pack routines: a pack's cron prompt runs through the agent (with that pack's skill) and is pushed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from orbit.core.agent import NO_MESSAGE

if TYPE_CHECKING:
    from orbit.app import OrbitApp
    from orbit.packs import Routine


async def run_routine(orbit: OrbitApp, pack: str, routine: Routine) -> str:
    """Run one routine and push the reply to the owner's Telegram chat (or wherever push falls back to).

    A reply of exactly NO_MESSAGE (the routine decided there is nothing to say today) is not pushed.

    The reply is also added to that chat's history, so answering it in Telegram continues the thread.
    """
    telegram = orbit.channels.get("telegram")
    owner = telegram.default_chat_id() if telegram else None
    channel, chat_id = ("telegram", owner) if owner else ("cli", "local")
    reply = await orbit.agent.run_routine(pack, routine.prompt, channel, chat_id)
    if reply.strip().strip("。.").strip() == NO_MESSAGE:
        logger.info("routine {} ({}) ran: nothing to send", pack, routine.cron)
        return ""
    delivered = await orbit.push(reply)
    if owner:
        await orbit.db.add_message(orbit.agent.session_key("telegram", owner), "assistant", reply)
    status = "pushed" if delivered else "undelivered"
    logger.info("routine {} ({}) ran: {} chars, {}", pack, routine.cron, len(reply), status)
    return reply
