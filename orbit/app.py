"""OrbitApp wires everything together and runs forever."""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from typing import Any

from loguru import logger

from orbit import __version__
from orbit.channels.base import Channel, Incoming
from orbit.channels.local_api import LocalApiChannel
from orbit.config import Config, Settings, load_config
from orbit.core.agent import Agent
from orbit.core.memory import MemoryStore
from orbit.core.router import ModelRouter
from orbit.core.tools import registry
from orbit.radar import watcher as _radar_watcher  # noqa: F401 - registers the internship_radar watcher
from orbit.scheduler.scheduler import OrbitScheduler
from orbit.storage.db import Database


def setup_logging(settings: Settings) -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    if sys.stderr is not None:  # pythonw.exe (scheduled task) has no console
        logger.add(
            sys.stderr,
            level="INFO",
            format="<green>{time:HH:mm:ss}</green> <level>{level: <7}</level> {message}",
            diagnose=False,
        )
    logger.add(
        settings.logs_dir / "orbit.log",
        level="DEBUG",
        rotation="00:00",
        retention="14 days",
        encoding="utf-8",
        backtrace=False,
        diagnose=False,  # never dump local variables (API keys, messages) into the log file
    )


class OrbitApp:
    def __init__(self, settings: Settings | None = None, config: Config | None = None):
        self.settings = settings or Settings()
        self.config = config or load_config(self.settings.orbit_home / "config.yaml")
        self.started_at = datetime.now()
        self.db = Database(self.settings.database_url)
        self.db_ping_timeout = 2.0  # seconds; /health must answer even when Postgres hangs
        self.memory = MemoryStore(self.settings.memory_dir)
        self.router = ModelRouter.from_config(self.config, self.settings, usage_sink=self._record_usage)
        self.agent = Agent(
            self.config.agent, self.router, registry, self.memory, self.db, self.settings.persona_dir
        )
        self.scheduler = OrbitScheduler(self)
        self.agent.scheduler = self.scheduler
        self.channels: dict[str, Channel] = {}
        self._stop = asyncio.Event()

    async def _record_usage(self, task: str, provider: str, model: str, p: int, c: int) -> None:
        await self.db.add_usage(task, provider, model, p, c)

    # ---- lifecycle -------------------------------------------------------------
    async def start(self) -> None:
        logger.info("Orbit v{} starting (home={})", __version__, self.settings.orbit_home)
        await self.db.connect()
        await self.db.migrate()
        self.memory.ensure()
        if not self.router.providers:
            logger.error("no model provider configured - put OPENAI_API_KEY in .env")

        host, port = self.settings.api_bind(self.config.api)
        self.channels["cli"] = LocalApiChannel(self, host, port)
        if self.config.telegram.enabled:
            if self.settings.telegram_bot_token and self.settings.telegram_owner_chat_id:
                from orbit.channels.telegram import TelegramChannel

                self.channels["telegram"] = TelegramChannel(
                    self.settings.telegram_bot_token,
                    self.settings.telegram_owner_chat_id,
                    proxy=self.settings.proxy_url,
                )
            else:
                logger.warning("telegram enabled but TELEGRAM_BOT_TOKEN / TELEGRAM_OWNER_CHAT_ID missing - CLI only")

        for ch in self.channels.values():
            await ch.start(self.handle)
        await self.scheduler.start()

        if self.config.jobs.startup_notice and "telegram" in self.channels:
            await self.push(f"Orbit 已上线 ({datetime.now().strftime('%m-%d %H:%M')})")
        logger.info("Orbit is online. tools: {}", ", ".join(registry.names()))

    async def stop(self) -> None:
        logger.info("Orbit shutting down")
        await self.scheduler.stop()
        for ch in self.channels.values():
            try:
                await ch.stop()
            except Exception:
                logger.exception("error stopping channel {}", ch.name)
        await self.db.close()

    async def run(self) -> None:
        await self.start()
        try:
            await self._stop.wait()
        finally:
            await self.stop()

    def request_stop(self) -> None:
        self._stop.set()

    # ---- message flow ----------------------------------------------------------
    async def handle(self, incoming: Incoming) -> str:
        logger.info("[{}:{}] user: {}", incoming.channel, incoming.chat_id, incoming.text[:200])
        reply = await self.agent.handle(incoming.channel, incoming.chat_id, incoming.text)
        logger.info("[{}:{}] orbit: {}", incoming.channel, incoming.chat_id, reply[:200])
        return reply

    async def push(self, text: str, channel: str | None = None, chat_id: str | None = None) -> bool:
        """Deliver a proactive message. Falls back to Telegram owner if the origin channel can't push."""
        candidates: list[tuple[Channel, str]] = []
        if channel and chat_id and channel in self.channels:
            candidates.append((self.channels[channel], chat_id))
        for ch in self.channels.values():
            target = ch.default_chat_id()
            if target and (ch.name, target) not in [(c.name, t) for c, t in candidates]:
                candidates.append((ch, target))
        for ch, target in candidates:
            try:
                if await ch.send(target, text):
                    return True
            except Exception:
                logger.exception("push via {} failed", ch.name)
        logger.warning("push undelivered: {}", text[:120])
        return False

    def uptime_seconds(self) -> int:
        return int((datetime.now() - self.started_at).total_seconds())

    async def db_health(self) -> dict[str, Any]:
        """Ping the database with a timeout. Never raises.

        Returns {"db": "ok"|"error", "db_latency_ms": float} plus "error" (a short string) on failure.
        """
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(self.db.ping(), timeout=self.db_ping_timeout)
        except Exception as e:  # timeout, refused connection, auth failure, not connected
            latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            detail = str(e).splitlines()[0] if str(e) else ""
            error = f"{type(e).__name__}: {detail}" if detail else type(e).__name__
            logger.warning("db health check failed after {} ms: {}", latency_ms, error)
            return {"db": "error", "db_latency_ms": latency_ms, "error": error[:200]}
        return {"db": "ok", "db_latency_ms": round((time.perf_counter() - t0) * 1000, 1)}

    async def status(self) -> dict:
        db = await self.db_health()
        pending = await self.db.list_reminders("pending")
        return {
            "version": __version__,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "uptime_seconds": self.uptime_seconds(),
            "db": db["db"],
            "channels": list(self.channels),
            "providers": list(self.router.providers),
            "tasks": {k: f"{v.provider}:{v.model}" for k, v in self.config.tasks.items()},
            "tools": registry.names(),
            "pending_reminders": len(pending),
            "jobs": self.scheduler.jobs(),
            "usage_today": await self.db.usage_today(),
            "memory_entries": len(self.memory.entries()),
        }


def main() -> None:
    settings = Settings()
    setup_logging(settings)
    app = OrbitApp(settings)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
