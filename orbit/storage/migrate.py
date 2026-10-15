"""Run Alembic migrations from inside Orbit (startup and tests), without alembic.ini."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(connection: Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["configure_logger"] = False  # keep loguru / pytest logging untouched
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


async def upgrade_head(engine: AsyncEngine) -> None:
    def _run(conn: Connection) -> None:
        command.upgrade(alembic_config(conn), "head")

    async with engine.begin() as conn:
        await conn.run_sync(_run)
