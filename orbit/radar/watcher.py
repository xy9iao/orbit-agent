"""The Internship Radar as an Orbit watcher (M3.3).

Enabled in config.yaml:

    watchers:
      - { type: internship_radar, interval_minutes: 30, first_run_delay_seconds: 60 }

The scheduler constructs the watcher, calls `bind(orbit)` (we keep the app for its config, router and
db, and open one long-lived `PoliteClient`, so per-host spacing holds across polls), then calls
`check(db)` every interval and `close()` on shutdown. `check` runs `orbit.radar.pipeline.run_pipeline`
(poll, extract, embed, score, notify), which never raises, logs one summary line and returns the
notification events (M6.1). The scheduler pushes each one through `OrbitApp.push` and calls
`on_pushed(event, ok)`, which records the result on the postings' `radar_notifications` rows (`sent`,
or `failed` to be retried on a later run; see orbit.radar.notify).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from loguru import logger

from orbit.config import RadarConfig
from orbit.radar.notify import record_delivery
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
        self._db: Database | None = None  # the db the last check ran on; on_pushed records results there

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
        self._db = db
        summary = await run_pipeline(db, router, self._ensure_client(), config, notify=True)
        logger.info("radar watcher: {}", describe(summary))
        return summary.events

    async def on_pushed(self, event: Event, ok: bool) -> None:
        """Record the push result on every posting the event carried (sent, or failed for a retry)."""
        posting_ids = [int(i) for i in event.data.get("posting_ids") or []]
        db = self._db if self._db is not None else (self.orbit.db if self.orbit is not None else None)
        if not posting_ids or db is None:
            return
        await record_delivery(db, posting_ids, ok)
        if not ok:
            logger.warning(
                "radar watcher: {} push failed for {} posting(s); retried on a later run",
                event.data.get("kind", "?"),
                len(posting_ids),
            )
