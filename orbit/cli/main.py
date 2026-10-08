"""`orbit` command line: talk to the running daemon over the local API."""

from __future__ import annotations

import json
import sys

import httpx
import typer

from orbit.config import load_config

app = typer.Typer(help="Orbit - your always-on personal agent.", no_args_is_help=True)


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


if __name__ == "__main__":
    sys.exit(app())
