"""Watcher interface (phase 2 will add concrete ones: gmail, rss, web diff...).

To add a watcher:
    @register_watcher("rss")
    class RssWatcher(Watcher):
        def __init__(self, url: str): ...
        async def check(self, db) -> list[Event]: ...
then enable it in config.yaml:
    watchers:
      - { type: rss, url: https://..., interval_minutes: 30 }

Optional lifecycle hooks (default no-ops, so a watcher that only defines `check` keeps working):
    bind(orbit)  - called once by the scheduler right after construction, with the OrbitApp
                   (settings, config, router, db), before the first check;
    close()      - awaited by the scheduler on shutdown to release clients or connections.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orbit.app import OrbitApp
    from orbit.storage.db import Database


@dataclass
class Event:
    title: str
    body: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class Watcher:
    """Polled periodically; returns only *new* events. Use db.kv_get/kv_set to remember what was seen."""

    name: str = "watcher"

    def bind(self, orbit: OrbitApp) -> None:
        """Give the watcher the running app. Default: ignore it."""

    async def close(self) -> None:
        """Release resources on shutdown. Default: nothing to release."""

    async def check(self, db: Database) -> list[Event]:
        raise NotImplementedError


WATCHER_TYPES: dict[str, type[Watcher]] = {}


def register_watcher(type_name: str) -> Callable[[type[Watcher]], type[Watcher]]:
    def deco(cls: type[Watcher]) -> type[Watcher]:
        cls.name = type_name
        WATCHER_TYPES[type_name] = cls
        return cls

    return deco
