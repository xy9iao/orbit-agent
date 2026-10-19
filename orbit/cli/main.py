"""`orbit` command line: talk to the running daemon over the local API."""

from __future__ import annotations

import json
import sys

import httpx
import typer

from orbit.config import load_config
from orbit.radar.cli import radar_app

app = typer.Typer(help="Orbit - your always-on personal agent.", no_args_is_help=True)
app.add_typer(radar_app, name="radar")


def _base_url() -> str:
    cfg = load_config()
    return f"http://{cfg.api.host}:{cfg.api.port}"


def _client() -> httpx.Client:
    return httpx.Client(base_url=_base_url(), timeout=300)


def _die_if_offline(e: httpx.HTTPError) -> None:
    typer.secho(f"Orbit 没在运行？({e.__class__.__name__}) 先执行: orbit serve", fg="red")
    raise typer.Exit(1)


@app.command()
def serve() -> None:
    """Start the always-on daemon in the foreground (Ctrl+C to stop)."""
    from orbit.app import main

    main()


@app.command()
def chat(chat_id: str = "local") -> None:
    """Interactive chat with Orbit (shares memory with Telegram). /quit to exit, /clear to reset history."""
    typer.secho("Orbit chat - /quit 退出, /clear 清空本会话历史", fg="cyan")
    with _client() as c:
        while True:
            try:
                text = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not text:
                continue
            if text in ("/quit", "/exit", "/q"):
                break
            if text == "/clear":
                c.post("/history/clear", params={"chat_id": chat_id})
                typer.secho("(history cleared)", fg="yellow")
                continue
            try:
                r = c.post("/chat", json={"text": text, "chat_id": chat_id})
                r.raise_for_status()
            except httpx.HTTPError as e:
                _die_if_offline(e)
            typer.secho("orbit> ", fg="green", nl=False)
            print(r.json()["reply"])


@app.command()
def send(text: str, chat_id: str = "local") -> None:
    """Send one message and print the reply."""
    with _client() as c:
        try:
            r = c.post("/chat", json={"text": text, "chat_id": chat_id})
            r.raise_for_status()
        except httpx.HTTPError as e:
            _die_if_offline(e)
        print(r.json()["reply"])


@app.command()
def push(text: str) -> None:
    """Push a raw message to the default channel (Telegram), without the model."""
    with _client() as c:
        try:
            r = c.post("/push", json={"text": text})
            r.raise_for_status()
        except httpx.HTTPError as e:
            _die_if_offline(e)
        print("delivered" if r.json()["delivered"] else "undelivered (no push-capable channel)")


@app.command()
def status() -> None:
    """Show daemon status: uptime, channels, models, jobs, today's usage."""
    with _client() as c:
        try:
            r = c.get("/status")
            r.raise_for_status()
        except httpx.HTTPError as e:
            _die_if_offline(e)
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))


@app.command()
def reminders() -> None:
    """List pending reminders."""
    with _client() as c:
        try:
            r = c.get("/reminders")
            r.raise_for_status()
        except httpx.HTTPError as e:
            _die_if_offline(e)
        rows = r.json()
        if not rows:
            print("no pending reminders")
        for row in rows:
            print(f"#{row['id']}  {row['due_at'].replace('T', ' ')}  {row['text']}  [{row['channel']}]")


@app.command()
def cancel(reminder_id: int) -> None:
    """Cancel a reminder by id."""
    with _client() as c:
        try:
            r = c.post(f"/reminders/{reminder_id}/cancel")
            r.raise_for_status()
        except httpx.HTTPError as e:
            _die_if_offline(e)
        print(f"cancelled #{reminder_id}")


@app.command()
def jobs() -> None:
    """List scheduled jobs and their next run time."""
    with _client() as c:
        try:
            r = c.get("/jobs")
            r.raise_for_status()
        except httpx.HTTPError as e:
            _die_if_offline(e)
        for j in r.json():
            print(f"{j['id']:<24} next: {j['next_run']}")


@app.command()
def briefing() -> None:
    """Generate and push today's briefing right now."""
    with _client() as c:
        try:
            r = c.post("/briefing")
            r.raise_for_status()
        except httpx.HTTPError as e:
            _die_if_offline(e)
        print(r.json()["briefing"])


# ---- data ownership: these open the database directly (they work while the daemon is down) -----------


async def _with_db(fn):
    from orbit.config import Settings
    from orbit.storage.db import Database

    settings = Settings()
    config = load_config(settings.orbit_home / "config.yaml")
    db = Database(settings.database_url)
    await db.connect()
    try:
        await db.migrate()
        return await fn(db, settings, config)
    finally:
        await db.close()


@app.command()
def export(
    out: str | None = typer.Option(None, "--out", help="Folder to write (default: EXPORT_DIR/manual-<time>)"),
) -> None:
    """Write a plain-file snapshot of records, skills, packs, memory, persona, reminders, messages and media."""
    import asyncio
    from datetime import datetime
    from pathlib import Path

    from orbit.export import export_snapshot

    async def run(db, settings, config):
        target = Path(out) if out else settings.exports_dir / f"manual-{datetime.now():%Y%m%d-%H%M%S}"
        return target, await export_snapshot(db, settings, config, target)

    target, manifest = asyncio.run(_with_db(run))
    counts = manifest["counts"]
    records = ", ".join(f"{k} {v}" for k, v in counts["records"].items()) or "none"
    print(f"exported to {target}")
    print(f"records: {records}; reminders {counts['reminders']}, messages {counts['messages']}, "
          f"media files {counts['media_files']}")


@app.command("import")
def import_(src: str = typer.Argument(..., help="A folder written by `orbit export`")) -> None:
    """Restore records, skills, missing packs, memory and media from an export (rows already present are skipped)."""
    import asyncio
    from pathlib import Path

    from orbit.export import import_snapshot

    try:
        result = asyncio.run(_with_db(lambda db, settings, config: import_snapshot(db, settings, config, Path(src))))
    except FileNotFoundError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1) from None
    print(
        f"imported records {result['records']} (skipped {result['records_skipped']} already present), "
        f"skills {result['skills']}, packs {result['packs']}, memory lines {result['memory_lines']}, "
        f"media files {result['media_files']}"
    )


if __name__ == "__main__":
    sys.exit(app())
