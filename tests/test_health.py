"""GET /health and the "db" key in /status, through the real FastAPI app over ASGI (no sockets)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from orbit import __version__
from orbit.app import OrbitApp
from orbit.channels.local_api import LocalApiChannel, build_api
from orbit.config import Settings
from orbit.storage.db import Database

from .conftest import make_config

UNREACHABLE_URL = "postgresql+asyncpg://orbit:orbit@127.0.0.1:1/orbit_test_unreachable"


def make_app(tmp_path: Path, db: Database) -> OrbitApp:
    """An OrbitApp that is constructed but never started (no channels, no scheduler, no network)."""
    settings = Settings(_env_file=None, orbit_home=tmp_path, database_url=db.url, openai_api_key=None)
    app = OrbitApp(settings, make_config())
    app.db = db
    app.agent.db = db  # the Agent captured the unconnected original at construction
    app.memory.ensure()
    return app


async def get(app: OrbitApp, path: str) -> httpx.Response:
    api = build_api(app, LocalApiChannel(app, "127.0.0.1", 0))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://orbit") as client:
        return await client.get(path)


class HangingDatabase(Database):
    async def ping(self) -> None:
        await asyncio.sleep(3600)


async def test_health_ok(tmp_path: Path, db: Database) -> None:
    resp = await get(make_app(tmp_path, db), "/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert isinstance(body["db_latency_ms"], float) and body["db_latency_ms"] >= 0
    assert body["version"] == __version__
    assert isinstance(body["uptime_seconds"], int) and body["uptime_seconds"] >= 0
    assert "error" not in body


async def test_health_db_unreachable_is_503(tmp_path: Path) -> None:
    down = Database(UNREACHABLE_URL)
    await down.connect()  # lazy: creates the engine without opening a connection
    app = make_app(tmp_path, down)
    # Long enough for a refused connect to surface itself (Windows retries a refused SYN for ~2 s);
    # the timeout path has its own test below.
    app.db_ping_timeout = 10
    try:
        resp = await get(app, "/health")
    finally:
        await down.close()

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] == "error"
    assert body["error"] and len(body["error"]) <= 200
    assert not body["error"].startswith("TimeoutError")
    assert body["db_latency_ms"] >= 0
    assert body["version"] == __version__


async def test_health_times_out_instead_of_hanging(tmp_path: Path) -> None:
    app = make_app(tmp_path, HangingDatabase(UNREACHABLE_URL))
    app.db_ping_timeout = 0.05

    resp = await asyncio.wait_for(get(app, "/health"), timeout=5)

    assert resp.status_code == 503
    body = resp.json()
    assert body["db"] == "error"
    assert body["error"].startswith("TimeoutError")
    assert body["db_latency_ms"] < 5000


async def test_health_db_not_connected_is_503(tmp_path: Path) -> None:
    resp = await get(make_app(tmp_path, Database(UNREACHABLE_URL)), "/health")

    assert resp.status_code == 503
    assert resp.json()["db"] == "error"


async def test_status_includes_db(tmp_path: Path, db: Database) -> None:
    resp = await get(make_app(tmp_path, db), "/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["db"] == "ok"
    for key in ("version", "started_at", "uptime_seconds", "channels", "providers", "tasks", "tools",
                "pending_reminders", "jobs", "usage_today", "memory_entries"):
        assert key in body

