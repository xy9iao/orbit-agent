"""`orbit radar ...`: Internship Radar commands.

Unlike the rest of the `orbit` CLI, these do not call the daemon's HTTP API. Each command opens its own
DB engine and model router from .env/config.yaml (PLAN.md Decisions: "Radar CLI transport"), so they
work while the daemon is down.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from sqlalchemy.exc import SQLAlchemyError

from orbit.config import Config, Settings, load_config
from orbit.core.router import ModelRouter
from orbit.storage.db import Database

radar_app = typer.Typer(
    help="Internship Radar commands (run directly against the DB, not via the daemon)",
    no_args_is_help=True,
)


@dataclass
class RadarRuntime:
    settings: Settings
    config: Config
    db: Database
    router: ModelRouter


@asynccontextmanager
async def default_runtime(settings: Settings | None = None) -> AsyncIterator[RadarRuntime]:
    """Connect + migrate the DB and build the router; model usage is recorded like the daemon does."""
    settings = settings or Settings()
    config = load_config(settings.orbit_home / "config.yaml")
    db = Database(settings.database_url)
    await db.connect()
    try:
        await db.migrate()
        router = ModelRouter.from_config(config, settings, usage_sink=db.add_usage)
        yield RadarRuntime(settings, config, db, router)
    finally:
        await db.close()


# Commands call `open_runtime()` through this module attribute, so tests can swap in a test DB and a
# router built on FakeProvider with monkeypatch.setattr("orbit.radar.cli.open_runtime", ...).
open_runtime: Callable[..., AbstractAsyncContextManager[RadarRuntime]] = default_runtime


def _fail(msg: str) -> typer.Exit:
    typer.secho(msg, fg="red", err=True)
    return typer.Exit(1)


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """asyncio.run with the expected failures turned into a one-line error and exit code 1."""
    try:
        return asyncio.run(coro)
    except (FileNotFoundError, ValueError) as e:
        raise _fail(str(e)) from None
    except (OSError, SQLAlchemyError) as e:
        first_line = (str(e).splitlines() or [""])[0]
        raise _fail(f"database error ({e.__class__.__name__}): {first_line}") from None


def require_task(router: ModelRouter, task: str) -> None:
    """Fail with a clear message if the provider serving `task` has no API key."""
    tc = router.config.tasks.get(task) or router.config.tasks.get("chat")
    if tc is not None and tc.provider in router.providers:
        return
    if tc is None:
        raise _fail(f"config.yaml has no '{task}' task (and no 'chat' fallback)")
    pc = router.config.providers.get(tc.provider)
    env = pc.api_key_env if pc else "the provider's API key"
    raise _fail(f"no provider for the '{task}' task: '{tc.provider}' needs {env} set in .env or the environment")


@radar_app.command()
def resume(
    path: Path | None = typer.Argument(  # noqa: B008
        None, help="Resume as .md or .txt (default: data/resume.md under ORBIT_HOME)"
    ),
) -> None:
    """Embed your resume and store it as the radar profile (skipped if the text is unchanged)."""
    from orbit.radar.profile import load_resume, resolve_resume_path, set_resume

    resume_path = path if path is not None else resolve_resume_path(None, Settings().orbit_home)
    try:
        load_resume(resume_path)  # fail fast on a missing or unsupported file, before touching the DB
    except (FileNotFoundError, ValueError) as e:
        raise _fail(str(e)) from None

    async def _main():
        async with open_runtime() as rt:
            require_task(rt.router, "embed")
            return await set_resume(rt.db, rt.router, resume_path)

    result = _run(_main())
    action = "embedded" if result.changed else "unchanged, not re-embedded"
    typer.echo(f"resume {action}: {result.chars} chars, sha256 {result.text_hash[:12]} ({resume_path})")
    if result.truncated:
        typer.secho("note: resume is very long; only the first part was embedded (full text stored)", fg="yellow")
