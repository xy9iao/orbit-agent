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
from typing import TYPE_CHECKING, Any

import typer
from sqlalchemy.exc import SQLAlchemyError

from orbit.config import Config, Settings, load_config
from orbit.core.router import ModelRouter
from orbit.storage.db import Database

if TYPE_CHECKING:
    from orbit.radar.sources import PoliteClient

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


@radar_app.command()
def seed(
    file: Path | None = typer.Option(  # noqa: B008
        None, "--file", "-f", help="Seeds YAML (default: orbit/radar/seeds.yaml shipped with Orbit)"
    ),
    prune: bool = typer.Option(False, "--prune", help="Deactivate DB companies that are no longer in the file"),
) -> None:
    """Upsert the seed companies into the DB by slug (never deletes; inactive companies stay inactive)."""
    from orbit.radar.seed import DEFAULT_SEEDS_PATH, load_seeds, seed_companies

    path = file if file is not None else DEFAULT_SEEDS_PATH
    try:
        seeds = load_seeds(path)  # validate before touching the DB
    except (FileNotFoundError, ValueError) as e:
        raise _fail(str(e)) from None

    async def _main():
        async with open_runtime() as rt, rt.db.session() as session:
            result = await seed_companies(session, seeds, prune=prune)
            await session.commit()
            return result

    r = _run(_main())
    typer.echo(
        f"seeded {len(seeds)} companies from {path}: {len(r.inserted)} inserted, {len(r.updated)} updated, "
        f"{len(r.unchanged)} unchanged"
    )
    if r.deactivated:
        typer.secho(f"deactivated (not in file): {', '.join(r.deactivated)}", fg="yellow")
    kept = [s for s in r.missing if s not in r.deactivated]
    if kept:
        typer.secho(f"in the DB but not in the file (left as is; --prune deactivates): {', '.join(kept)}", fg="yellow")


def make_http_client() -> PoliteClient:
    """The job-board HTTP client; tests swap this for one on httpx.MockTransport."""
    from orbit.radar.sources import PoliteClient

    return PoliteClient()


@radar_app.command("verify-sources")
def verify_sources_cmd(
    source: str | None = typer.Option(None, "--source", "-s", help="Only check one board type"),
    min_ok: int | None = typer.Option(
        None, "--min-ok", help="Exit 1 if fewer companies pass (default: 20, or 1 with --source)"
    ),
) -> None:
    """Fetch every active company's board live, report ok/error and posting counts, store last_verify_*."""
    from orbit.radar.verify import format_report, verify_sources

    threshold = min_ok if min_ok is not None else (20 if source is None else 1)

    async def _main():
        async with open_runtime() as rt, make_http_client() as client:
            return await verify_sources(rt.db, client, source=source)

    report = _run(_main())
    typer.echo(format_report(report, threshold))
    if report.ok_count < threshold:
        raise typer.Exit(1)
