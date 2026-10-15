"""InternshipRadarWatcher and the watcher hooks (M3.3): scheduler wiring, bind/close, config.

Boards are served by httpx.MockTransport; the autouse `no_network` guard (from test_poll) fails any
request that escapes it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from tzlocal import get_localzone

from orbit.app import OrbitApp
from orbit.config import RadarConfig, load_config
from orbit.radar.profile import DEFAULT_RESUME_PATH
from orbit.radar.sources.http import PoliteClient
from orbit.radar.watcher import InternshipRadarWatcher
from orbit.scheduler.watchers.base import WATCHER_TYPES, Event, Watcher
from orbit.storage.db import Database
from orbit.storage.models import Company, Posting, Run

from ..conftest import ROOT
from ..test_health import make_app
from .test_poll import FakeBoards, acme_payload, no_network  # noqa: F401 - no_network is an autouse fixture

RADAR_JOB = "watcher-internship_radar"


async def _no_sleep(_seconds: float) -> None:
    return None


def app_with_watchers(tmp_path: Path, db: Database, *watchers: dict[str, Any]) -> OrbitApp:
    app = make_app(tmp_path, db)
    app.config.watchers = [dict(w) for w in watchers]
    app.config.jobs.briefing.enabled = False
    return app


def _now() -> datetime:
    return datetime.now(get_localzone())


async def _seed_acme(db: Database) -> Company:
    async with db.session() as s:
        company = Company(slug="acme", name="Acme", source="greenhouse", board_token="acme", active=True)
        s.add(company)
        await s.commit()
        return company


# ---- scheduler registration -------------------------------------------------------------------------


async def test_scheduler_registers_radar_job_every_30_minutes(tmp_path: Path, db: Database):
    app = app_with_watchers(tmp_path, db, {"type": "internship_radar", "interval_minutes": 30})
    await app.scheduler.start()
    try:
        job = app.scheduler.sched.get_job(RADAR_JOB)
        assert job is not None
        assert isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval == timedelta(minutes=30)
        assert job.max_instances == 1 and job.coalesce is True  # never two polls at once
        # Without first_run_delay_seconds the first run is one full interval away (old behavior).
        assert job.next_run_time > _now() + timedelta(minutes=29)
        assert RADAR_JOB in [j["id"] for j in app.scheduler.jobs()]

        [watcher] = app.scheduler.watchers
        assert isinstance(watcher, InternshipRadarWatcher)
        assert watcher.orbit is app  # bind() was called
        assert isinstance(watcher.client, PoliteClient)
        http = watcher.client._client
    finally:
        await app.scheduler.stop()
    assert watcher.client is None and http.is_closed  # stop() awaited close()


async def test_first_run_delay_schedules_the_first_poll_soon(tmp_path: Path, db: Database):
    app = app_with_watchers(
        tmp_path, db, {"type": "internship_radar", "interval_minutes": 30, "first_run_delay_seconds": 60}
    )
    await app.scheduler.start()
    try:
        job = app.scheduler.sched.get_job(RADAR_JOB)
        assert job.trigger.interval == timedelta(minutes=30)
        assert _now() + timedelta(seconds=30) < job.next_run_time < _now() + timedelta(seconds=90)
    finally:
        await app.scheduler.stop()


async def test_repo_config_enables_the_radar_watcher(tmp_path: Path, db: Database):
    app = make_app(tmp_path, db)
    app.config.watchers = load_config(ROOT / "config.yaml").watchers
    app.config.jobs.briefing.enabled = False
    await app.scheduler.start()
    try:
        job = app.scheduler.sched.get_job(RADAR_JOB)
        assert job is not None and job.trigger.interval == timedelta(minutes=30)
    finally:
        await app.scheduler.stop()


# ---- check / close ----------------------------------------------------------------------------------


async def test_check_polls_active_companies_and_returns_no_events(tmp_path: Path, db: Database):
    acme = await _seed_acme(db)
    boards = FakeBoards(acme=acme_payload())
    watcher = InternshipRadarWatcher(
        client_factory=lambda: PoliteClient(transport=httpx.MockTransport(boards), sleep=_no_sleep)
    )
    watcher.bind(make_app(tmp_path, db))
    try:
        events = await watcher.check(db)
    finally:
        await watcher.close()

    assert events == []
    assert boards.tokens_requested() == ["acme"]
    async with db.session() as s:
        postings = list((await s.execute(select(Posting).where(Posting.company_id == acme.id))).scalars())
        [run] = (await s.execute(select(Run).where(Run.company_id == acme.id))).scalars()
    assert [p.external_id for p in postings] == ["9001"]
    assert run.ok is True and run.new == 1


async def test_scheduler_poll_of_radar_pushes_nothing(tmp_path: Path, db: Database, monkeypatch):
    await _seed_acme(db)
    boards = FakeBoards(acme=acme_payload())
    app = make_app(tmp_path, db)
    pushed: list[str] = []

    async def fake_push(text: str, channel: str | None = None, chat_id: str | None = None) -> bool:
        pushed.append(text)
        return True

    monkeypatch.setattr(app, "push", fake_push)
    watcher = InternshipRadarWatcher(
        client_factory=lambda: PoliteClient(transport=httpx.MockTransport(boards), sleep=_no_sleep)
    )
    watcher.bind(app)
    try:
        await app.scheduler._poll_watcher(watcher)
    finally:
        await watcher.close()
    assert boards.tokens_requested() == ["acme"] and pushed == []


async def test_failing_board_does_not_raise(tmp_path: Path, db: Database):
    await _seed_acme(db)
    boards = FakeBoards(acme=404)
    watcher = InternshipRadarWatcher(
        client_factory=lambda: PoliteClient(transport=httpx.MockTransport(boards), sleep=_no_sleep)
    )
    watcher.bind(make_app(tmp_path, db))
    try:
        assert await watcher.check(db) == []
    finally:
        await watcher.close()
    async with db.session() as s:
        [run] = (await s.execute(select(Run))).scalars()
    assert run.ok is False and "404" in run.error


async def test_close_closes_the_client_and_is_idempotent(tmp_path: Path, db: Database):
    watcher = InternshipRadarWatcher()
    await watcher.close()  # never bound: nothing to close
    watcher.bind(make_app(tmp_path, db))
    http = watcher.client._client
    assert not http.is_closed
    await watcher.close()
    assert http.is_closed and watcher.client is None
    await watcher.close()


def test_unknown_config_keys_are_ignored():
    watcher = InternshipRadarWatcher(url="https://example.invalid")
    assert watcher.client is None and watcher.orbit is None


# ---- backwards compatibility ------------------------------------------------------------------------


class LegacyWatcher(Watcher):
    """A watcher written before bind/close existed: only __init__ and check."""

    name = "legacy_dummy"

    def __init__(self, label: str):
        self.label = label
        self.checks = 0

    async def check(self, db: Database) -> list[Event]:
        self.checks += 1
        return [Event(title=f"{self.label} #{self.checks}")]


async def test_watcher_without_hooks_still_works(tmp_path: Path, db: Database, monkeypatch):
    monkeypatch.setitem(WATCHER_TYPES, "legacy_dummy", LegacyWatcher)
    app = app_with_watchers(tmp_path, db, {"type": "legacy_dummy", "interval_minutes": 5, "label": "hi"})
    pushed: list[str] = []

    async def fake_push(text: str, channel: str | None = None, chat_id: str | None = None) -> bool:
        pushed.append(text)
        return True

    monkeypatch.setattr(app, "push", fake_push)
    await app.scheduler.start()
    try:
        job = app.scheduler.sched.get_job("watcher-legacy_dummy")
        assert job.trigger.interval == timedelta(minutes=5)
        assert job.next_run_time > _now() + timedelta(minutes=4)
        [watcher] = app.scheduler.watchers
        await app.scheduler._poll_watcher(watcher)
    finally:
        await app.scheduler.stop()  # base close() is a no-op
    assert pushed == ["[legacy_dummy] hi #1"]


class BrokenWatcher(Watcher):
    name = "broken_dummy"

    def bind(self, orbit: OrbitApp) -> None:
        raise RuntimeError("cannot bind")


class BadCloseWatcher(Watcher):
    name = "bad_close_dummy"

    async def check(self, db: Database) -> list[Event]:
        return []

    async def close(self) -> None:
        raise RuntimeError("cannot close")


async def test_bind_and_close_failures_never_crash_the_scheduler(tmp_path: Path, db: Database, monkeypatch):
    monkeypatch.setitem(WATCHER_TYPES, "broken_dummy", BrokenWatcher)
    monkeypatch.setitem(WATCHER_TYPES, "bad_close_dummy", BadCloseWatcher)
    app = app_with_watchers(tmp_path, db, {"type": "broken_dummy"}, {"type": "bad_close_dummy"})
    await app.scheduler.start()
    try:
        ids = [j["id"] for j in app.scheduler.jobs()]
        assert "watcher-broken_dummy" not in ids  # skipped, logged
        assert "watcher-bad_close_dummy" in ids
    finally:
        await app.scheduler.stop()  # the close() error is logged, not raised


# ---- config -----------------------------------------------------------------------------------------


def test_radar_config_defaults():
    cfg = RadarConfig()
    assert cfg.terms and all(re.fullmatch(r"(Winter|Spring|Summer|Fall) \d{4}", t) for t in cfg.terms)
    assert cfg.locations and all(
        re.fullmatch(r"[^,]+, [A-Z]{2}", loc) or loc == "Remote (Canada)" for loc in cfg.locations
    )
    assert 0 < cfg.notify_threshold < 1
    assert cfg.resume_path == DEFAULT_RESUME_PATH
    assert (cfg.max_extract_per_poll, cfg.max_notifications_per_poll) == (40, 5)


def test_config_without_radar_section_gets_defaults(config):
    assert config.radar == RadarConfig()


def test_repo_config_yaml_parses_with_radar_section():
    cfg = load_config(ROOT / "config.yaml")
    assert isinstance(cfg.radar, RadarConfig)
    assert cfg.radar.terms and cfg.radar.locations
    assert {"type": "internship_radar", "interval_minutes": 30}.items() <= cfg.watchers[0].items()


def test_radar_config_rejects_bad_values():
    with pytest.raises(ValueError):
        RadarConfig(notify_threshold=2.0)
    with pytest.raises(ValueError):
        RadarConfig(max_notifications_per_poll=-1)
