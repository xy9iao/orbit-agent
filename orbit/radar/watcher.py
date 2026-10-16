"""The Internship Radar as an Orbit watcher (M3.3).

Enabled in config.yaml:

    watchers:
      - { type: internship_radar, interval_minutes: 30, first_run_delay_seconds: 60 }

The scheduler constructs the watcher, calls `bind(orbit)` (we keep the app for its config, router and
db, and open one long-lived `PoliteClient`, so per-host spacing holds across polls), then calls
`check(db)` every interval and `close()` on shutdown. `check` runs `orbit.radar.pipeline.run_pipeline`
(poll, then extraction; embedding, scoring and notifications join it in M5.3/M5.4/M6.1), which never
raises, and logs one summary line.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from loguru import logger

from orbit.config import RadarConfig
from orbit.radar.pipeline import describe, run_pipeline
from orbit.radar.sources.http import PoliteClient
from orbit.scheduler.watchers.base import Event, Watcher, register_watcher

if TYPE_CHECKING:
    from orbit.app import OrbitApp
    from orbit.storage.db import Database


@register_watcher("internship_radar")
class InternshipRadarWatcher(Watcher):
    def __init__(self, *, client_factory: Callable[[], PoliteClient] = PoliteClient, **unknown: Any):
        # Every key of the config.yaml entry except type/interval_minutes/first_run_delay_seconds lands here.
        # Radar settings live in the `radar:` section, so extra keys are most likely typos: warn, don't crash.
        if unknown:
            logger.warning("internship_radar watcher: ignoring unknown config keys {}", sorted(unknown))
        self._client_factory = client_factory
        self.orbit: OrbitApp | None = None
        self.client: PoliteClient | None = None

    def bind(self, orbit: OrbitApp) -> None:
        self.orbit = orbit
        self._ensure_client()

    def _ensure_client(self) -> PoliteClient:
        if self.client is None:
            self.client = self._client_factory()
        return self.client

    async def close(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            await client.aclose()

    async def check(self, db: Database) -> list[Event]:
        router = self.orbit.router if self.orbit is not None else None
        config = self.orbit.config.radar if self.orbit is not None else RadarConfig()
        summary = await run_pipeline(db, router, self._ensure_client(), config)
        logger.info("radar watcher: {}", describe(summary))
        return []  # no events until notifications (M6.1)
