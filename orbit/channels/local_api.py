"""Local HTTP API on 127.0.0.1 - the CLI talks to the running daemon through this."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from orbit import __version__

from .base import Incoming, MessageHandler

if TYPE_CHECKING:
    from orbit.app import OrbitApp


class ChatRequest(BaseModel):
    text: str
    chat_id: str = "local"


class PushRequest(BaseModel):
    text: str


def build_api(orbit: OrbitApp, channel: LocalApiChannel) -> FastAPI:
    api = FastAPI(title="Orbit local API", version="0.1.0")

    @api.get("/health")
    async def health() -> JSONResponse:
        db = await orbit.db_health()
        ok = db["db"] == "ok"
        body = {
            "status": "ok" if ok else "degraded",
            **db,
            "version": __version__,
            "uptime_seconds": orbit.uptime_seconds(),
        }
        return JSONResponse(body, status_code=200 if ok else 503)

    @api.get("/status")
    async def status() -> dict:
        return await orbit.status()

    @api.post("/chat")
    async def chat(req: ChatRequest) -> dict:
        assert channel.handler is not None
        reply = await channel.handler(Incoming(channel=channel.name, chat_id=req.chat_id, text=req.text))
        return {"reply": reply}

    @api.post("/push")
    async def push(req: PushRequest) -> dict:
        ok = await orbit.push(req.text)
        return {"delivered": ok}

    @api.get("/reminders")
    async def reminders() -> list[dict]:
        return await orbit.db.list_reminders("pending")

    @api.post("/reminders/{reminder_id}/cancel")
    async def cancel(reminder_id: int) -> dict:
        ok = await orbit.db.set_reminder_status(reminder_id, "cancelled")
        if not ok:
            raise HTTPException(404, "no such reminder")
        orbit.scheduler.cancel_reminder(reminder_id)
        return {"cancelled": reminder_id}

    @api.get("/jobs")
    async def jobs() -> list[dict]:
        return orbit.scheduler.jobs()

    @api.post("/history/clear")
    async def clear_history(chat_id: str = "local") -> dict:
        await orbit.db.clear_history(f"{channel.name}:{chat_id}")
        return {"cleared": chat_id}

    @api.post("/briefing")
    async def briefing() -> dict:
        from orbit.scheduler.jobs.briefing import run_briefing

        text = await run_briefing(orbit)
        return {"briefing": text}

    return api


class LocalApiChannel:
    name = "cli"

    def __init__(self, orbit: OrbitApp, host: str, port: int):
        self.orbit = orbit
        self.host = host
        self.port = port
        self.handler: MessageHandler | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    def default_chat_id(self) -> str | None:
        return None  # the CLI cannot receive unsolicited pushes

    async def start(self, handler: MessageHandler) -> None:
        self.handler = handler
        config = uvicorn.Config(
            build_api(self.orbit, self), host=self.host, port=self.port, log_level="warning", loop="none"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        logger.info("local api: http://{}:{}", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                pass

    async def send(self, chat_id: str, text: str) -> bool:
        logger.info("push (no live CLI to deliver to): {}", text)
        return False
