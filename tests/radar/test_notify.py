"""Notifications (M6.1): claim, render, push through the real scheduler path, record the result.

Postings get their `score` set directly (no resume profile, so the pipeline's score stage leaves them
alone), the company is inactive (the poll stage fetches nothing), and the app has no model provider
(extract and embed are skipped). Delivery goes through `OrbitScheduler._poll_watcher` and
`OrbitApp.push` to a fake Telegram channel; nothing reaches the network.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update

from orbit.app import OrbitApp
from orbit.channels.base import MessageHandler
from orbit.config import RadarConfig
from orbit.radar import notify
from orbit.radar.notify import (
    DIGEST_MAX_LINES,
    EXPIRED_ERROR,
    MAX_ATTEMPTS,
    MAX_MESSAGE_CHARS,
    ClaimedMatch,
    build_events,
    claim_new_matches,
    record_delivery,
    render_digest,
    render_match,
)
from orbit.radar.pipeline import describe, run_pipeline
from orbit.radar.watcher import InternshipRadarWatcher
from orbit.scheduler.watchers.base import Event, Watcher
from orbit.storage.db import Database
from orbit.storage.models import Company, Notification, Posting
from tests.radar.test_match import _row
from tests.radar.test_poll import FakeBoards, make_client, no_network  # noqa: F401 - autouse network guard
from tests.test_health import make_app

PREFIX = "[internship_radar] "


class FakeTelegram:
    """Implements the Channel protocol; records every delivered text and can be told to fail."""

    name = "telegram"

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.calls = 0  # send() calls, delivered or not
        self.failing = False

    async def start(self, handler: MessageHandler) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, chat_id: str, text: str) -> bool:
        self.calls += 1
        if self.failing:
            return False
        self.sent.append(text)
        return True

    def default_chat_id(self) -> str | None:
        return "owner"


class Harness:
    def __init__(self, app: OrbitApp, channel: FakeTelegram, watcher: InternshipRadarWatcher, db: Database):
        self.app, self.channel, self.watcher, self.db = app, channel, watcher, db

    async def poll(self) -> list[str]:
        """One scheduled run of the radar watcher; returns the texts delivered by it."""
        before = len(self.channel.sent)
        await self.app.scheduler._poll_watcher(self.watcher)
        return self.channel.sent[before:]


@pytest.fixture
async def radar(tmp_path: Path, db: Database):
    app = make_app(tmp_path, db)
    channel = FakeTelegram()
    app.channels["telegram"] = channel
    watcher = InternshipRadarWatcher(client_factory=lambda: make_client(FakeBoards()))
    watcher.bind(app)
    try:
        yield Harness(app, channel, watcher, db)
    finally:
        await watcher.close()


async def _company(db: Database, name: str = "Acme AI") -> Company:
    async with db.session() as s:
        company = Company(slug=name.lower().replace(" ", "-"), name=name, source="greenhouse",
                          board_token=name.lower().replace(" ", ""), active=False)
        s.add(company)
        await s.commit()
        return company


async def _postings(db: Database, company: Company, scores: dict[str, float | None], **kw: Any) -> dict[str, int]:
    """Open, extracted postings with preset scores; returns {external_id: id}."""
    rows = [
        _row(company, key, minutes=i, score=score, title=f"{key.title()} ML Intern", **kw)
        for i, (key, score) in enumerate(scores.items())
    ]
    async with db.session() as s:
        s.add_all(rows)
        await s.commit()
        return {r.external_id: r.id for r in rows}


async def _set(db: Database, posting_id: int, **values: Any) -> None:
    async with db.session() as s:
        await s.execute(update(Posting).where(Posting.id == posting_id).values(**values))
        await s.commit()


async def _notifications(db: Database) -> dict[int, Notification]:
    async with db.session() as s:
        return {n.posting_id: n for n in (await s.execute(select(Notification))).scalars()}


def _titles(texts: list[str]) -> list[str]:
    return [t.splitlines()[0] for t in texts]


# ---- 1. once per posting ----------------------------------------------------------------------------


async def test_two_polls_push_each_match_exactly_once(radar: Harness):
    company = await _company(radar.db)
    ids = await _postings(radar.db, company, {"best": 0.9, "good": 0.7})

    first = await radar.poll()
    second = await radar.poll()

    assert _titles(first) == [
        f"{PREFIX}🎯 0.90 Acme AI — Best ML Intern",
        f"{PREFIX}🎯 0.70 Acme AI — Good ML Intern",
    ]
    assert first[0] == (
        f"{PREFIX}🎯 0.90 Acme AI — Best ML Intern\n"
        "Terms: Summer 2027, Fall 2027 · Locations: Toronto, ON\n"
        "https://example.com/best"
    )
    assert second == []
    rows = await _notifications(radar.db)
    assert set(rows) == set(ids.values())
    for posting_id in ids.values():
        row = rows[posting_id]
        assert (row.status, row.attempts, row.error) == ("sent", 1, None)
        assert row.sent_at is not None and row.sent_at >= row.created_at
    assert rows[ids["best"]].score == pytest.approx(0.9)


async def test_new_posting_after_a_poll_is_pushed_on_the_next_poll(radar: Harness):
    company = await _company(radar.db)
    await _postings(radar.db, company, {"best": 0.9})
    assert len(await radar.poll()) == 1
    await _postings(radar.db, company, {"later": 0.6})
    assert _titles(await radar.poll()) == [f"{PREFIX}🎯 0.60 Acme AI — Later ML Intern"]
    assert await radar.poll() == []


# ---- 2. retries -------------------------------------------------------------------------------------


async def test_failed_push_is_retried_on_the_next_poll_then_sent_once(radar: Harness):
    company = await _company(radar.db)
    ids = await _postings(radar.db, company, {"best": 0.9})

    radar.channel.failing = True
    assert await radar.poll() == []
    row = (await _notifications(radar.db))[ids["best"]]
    assert (row.status, row.attempts, row.sent_at) == ("failed", 1, None)
    assert row.error == notify.PUSH_FAILED_ERROR

    radar.channel.failing = False
    assert _titles(await radar.poll()) == [f"{PREFIX}🎯 0.90 Acme AI — Best ML Intern"]
    assert await radar.poll() == []
    row = (await _notifications(radar.db))[ids["best"]]
    assert (row.status, row.attempts, row.error) == ("sent", 2, None)
    assert radar.channel.calls == 2


async def test_never_more_than_three_attempts(radar: Harness):
    company = await _company(radar.db)
    ids = await _postings(radar.db, company, {"best": 0.9})
    radar.channel.failing = True

    for _ in range(5):
        await radar.poll()

    assert radar.channel.calls == MAX_ATTEMPTS == 3
    row = (await _notifications(radar.db))[ids["best"]]
    assert (row.status, row.attempts) == ("failed", 3)
    radar.channel.failing = False
    assert await radar.poll() == []


async def test_failed_posting_that_closed_or_dropped_is_not_retried(radar: Harness):
    company = await _company(radar.db)
    ids = await _postings(radar.db, company, {"closes": 0.9, "drops": 0.8})
    radar.channel.failing = True
    await radar.poll()
    radar.channel.failing = False
    await _set(radar.db, ids["closes"], status="closed")
    await _set(radar.db, ids["drops"], score=0.1)

    assert await radar.poll() == []
    await _set(radar.db, ids["drops"], score=0.85)  # back above the threshold, attempts left: retried
    assert _titles(await radar.poll()) == [f"{PREFIX}🎯 0.85 Acme AI — Drops ML Intern"]


# ---- 3. never re-notified ---------------------------------------------------------------------------


async def test_never_renotified_after_rescore_or_close_and_reopen(radar: Harness):
    company = await _company(radar.db)
    ids = await _postings(radar.db, company, {"rescored": 0.9, "reopened": 0.8})
    assert len(await radar.poll()) == 2

    await _set(radar.db, ids["rescored"], score=0.1)
    await _set(radar.db, ids["reopened"], status="closed", closed_at=datetime.now())
    assert await radar.poll() == []
    await _set(radar.db, ids["rescored"], score=0.95)
    await _set(radar.db, ids["reopened"], status="open", closed_at=None)
    assert await radar.poll() == []
    await _set(radar.db, ids["rescored"], score=None)
    await _set(radar.db, ids["rescored"], score=0.99)
    assert await radar.poll() == []
    assert len(radar.channel.sent) == 2


# ---- 4. threshold -----------------------------------------------------------------------------------


async def test_below_threshold_and_unscored_postings_are_never_notified(radar: Harness):
    radar.app.config.radar.notify_threshold = 0.5
    company = await _company(radar.db)
    ids = await _postings(radar.db, company, {"above": 0.5, "below": 0.49, "unscored": None})
    await _postings(radar.db, company, {"closed": 0.99}, status="closed")

    assert _titles(await radar.poll()) == [f"{PREFIX}🎯 0.50 Acme AI — Above ML Intern"]
    assert await radar.poll() == []
    assert set(await _notifications(radar.db)) == {ids["above"]}


# ---- 5. cap + digest --------------------------------------------------------------------------------


async def test_cap_and_one_digest_for_the_overflow(radar: Harness):
    radar.app.config.radar.max_notifications_per_poll = 2
    company = await _company(radar.db)
    scores = {f"p{i}": round(0.95 - i * 0.05, 2) for i in range(5)}  # p0 0.95 ... p4 0.75
    ids = await _postings(radar.db, company, scores)

    texts = await radar.poll()

    assert len(texts) == 3
    assert _titles(texts[:2]) == [f"{PREFIX}🎯 0.95 Acme AI — P0 ML Intern", f"{PREFIX}🎯 0.90 Acme AI — P1 ML Intern"]
    assert texts[2] == (
        f"{PREFIX}🗂 3 more radar matches\n"
        "Acme AI — P2 ML Intern — 0.85\n"
        "Acme AI — P3 ML Intern — 0.80\n"
        "Acme AI — P4 ML Intern — 0.75"
    )
    rows = await _notifications(radar.db)
    assert {rows[i].status for i in ids.values()} == {"sent"}
    assert await radar.poll() == []


async def test_digest_failure_retries_every_posting_it_carried(radar: Harness):
    radar.app.config.radar.max_notifications_per_poll = 1
    company = await _company(radar.db)
    ids = await _postings(radar.db, company, {"a": 0.9, "b": 0.8, "c": 0.7})
    radar.channel.failing = True
    await radar.poll()
    rows = await _notifications(radar.db)
    assert {(rows[i].status, rows[i].attempts) for i in ids.values()} == {("failed", 1)}

    radar.channel.failing = False
    texts = await radar.poll()
    assert _titles(texts) == [f"{PREFIX}🎯 0.90 Acme AI — A ML Intern", f"{PREFIX}🗂 2 more radar matches"]
    assert {n.status for n in (await _notifications(radar.db)).values()} == {"sent"}


async def test_long_digest_lists_fifteen_and_counts_the_rest(radar: Harness):
    radar.app.config.radar.max_notifications_per_poll = 1
    company = await _company(radar.db)
    await _postings(radar.db, company, {f"p{i:02d}": 0.99 - i * 0.01 for i in range(21)})

    texts = await radar.poll()

    assert len(texts) == 2
    digest = texts[1].splitlines()
    assert digest[0] == f"{PREFIX}🗂 20 more radar matches"
    assert len(digest) == 1 + DIGEST_MAX_LINES + 1
    assert digest[-1] == "and 5 more — run `orbit radar top`"
    assert len(await _notifications(radar.db)) == 21  # the counted-only ones are claimed too
    assert await radar.poll() == []


async def test_zero_cap_turns_notifications_off(radar: Harness):
    radar.app.config.radar.max_notifications_per_poll = 0
    company = await _company(radar.db)
    await _postings(radar.db, company, {"best": 0.9})
    assert await radar.poll() == []
    assert await _notifications(radar.db) == {}


def _match(i: int = 1, **kw: Any) -> ClaimedMatch:
    fields: dict[str, Any] = {
        "posting_id": i,
        "score": 0.62,
        "company": "Cohere",
        "title": "Machine Learning Intern",
        "terms": ["Summer 2027"],
        "locations": ["Toronto, ON", "Remote (Canada)"],
        "url": "https://jobs.example.com/cohere/1",
    }
    fields.update(kw)
    return ClaimedMatch(**fields)


def test_render_match_format():
    assert render_match(_match()) == (
        "🎯 0.62 Cohere — Machine Learning Intern",
        "Terms: Summer 2027 · Locations: Toronto, ON; Remote (Canada)\nhttps://jobs.example.com/cohere/1",
    )
    assert render_match(_match(terms=[], locations=[]))[1] == "https://jobs.example.com/cohere/1"
    assert render_match(_match(locations=[]))[1].startswith("Terms: Summer 2027\n")


def test_messages_stay_under_the_telegram_limit():
    huge = "Intern " * 2000
    title, body = render_match(_match(title=huge, terms=["Summer 2027"] * 500, url="https://x.example/" + "a" * 5000))
    assert len(PREFIX + title + "\n" + body) < 4096
    assert len(title) + 1 + len(body) <= MAX_MESSAGE_CHARS

    title, body = render_digest([_match(i, title=huge, company="C" * 500) for i in range(40)])
    assert len(PREFIX + title + "\n" + body) < 4096
    assert body.splitlines()[-1] == "and 25 more — run `orbit radar top`"


def test_build_events_data():
    events = build_events([_match(i) for i in range(1, 5)], 2)
    assert [e.data for e in events] == [
        {"posting_ids": [1], "kind": "match"},
        {"posting_ids": [2], "kind": "match"},
        {"posting_ids": [3, 4], "kind": "digest"},
    ]
    assert build_events([_match(1)], 5)[0].data == {"posting_ids": [1], "kind": "match"}
    assert build_events([], 5) == []


# ---- 6. crash safety --------------------------------------------------------------------------------


async def test_stale_pending_rows_are_not_resent(radar: Harness):
    company = await _company(radar.db)
    ids = await _postings(radar.db, company, {"crashed": 0.9, "inflight": 0.8})
    async with radar.db.session() as s:
        now = datetime.now().replace(microsecond=0)
        s.add_all([
            # claimed by a run whose process died before on_pushed: delivery unknown
            Notification(posting_id=ids["crashed"], score=0.9, status="pending", created_at=now - timedelta(hours=2)),
            # claimed a moment ago by another run that is still pushing
            Notification(posting_id=ids["inflight"], score=0.8, status="pending",
                         created_at=now - timedelta(minutes=5)),
        ])
        await s.commit()

    assert await radar.poll() == []
    assert await radar.poll() == []

    rows = await _notifications(radar.db)
    crashed = rows[ids["crashed"]]
    assert (crashed.status, crashed.attempts, crashed.error) == ("failed", MAX_ATTEMPTS, EXPIRED_ERROR)
    assert rows[ids["inflight"]].status == "pending"
    # the in-flight run's confirmation still lands, even after an expiry pass would have caught it
    await record_delivery(radar.db, [ids["inflight"], ids["crashed"]], True)
    rows = await _notifications(radar.db)
    assert (rows[ids["inflight"]].status, rows[ids["inflight"]].attempts) == ("sent", 1)
    assert (rows[ids["crashed"]].status, rows[ids["crashed"]].attempts) == ("sent", MAX_ATTEMPTS)
    assert radar.channel.calls == 0


async def test_record_delivery_never_downgrades_a_sent_row(db: Database):
    company = await _company(db)
    ids = await _postings(db, company, {"best": 0.9})
    [claimed] = await claim_new_matches(db, RadarConfig())
    assert await record_delivery(db, [claimed.posting_id], True) == 1
    assert await record_delivery(db, [claimed.posting_id], False) == 0
    row = (await _notifications(db))[ids["best"]]
    assert (row.status, row.attempts) == ("sent", 1)


# ---- 7. concurrent claims ---------------------------------------------------------------------------


async def test_concurrent_claims_claim_each_posting_once(db: Database):
    company = await _company(db)
    ids = await _postings(db, company, {f"p{i:02d}": 0.5 + i * 0.01 for i in range(30)})
    config = RadarConfig()

    first, second = await asyncio.gather(claim_new_matches(db, config), claim_new_matches(db, config))

    claimed = [m.posting_id for m in first + second]
    assert sorted(claimed) == sorted(ids.values())  # every posting once, none twice
    assert len(await _notifications(db)) == 30
    assert await claim_new_matches(db, config) == []


async def test_concurrent_retry_claims_claim_each_failed_row_once(db: Database):
    company = await _company(db)
    ids = await _postings(db, company, {f"p{i:02d}": 0.9 for i in range(10)})
    config = RadarConfig()
    claimed = await claim_new_matches(db, config)
    await record_delivery(db, [m.posting_id for m in claimed], False)

    first, second = await asyncio.gather(claim_new_matches(db, config), claim_new_matches(db, config))

    assert sorted(m.posting_id for m in first + second) == sorted(ids.values())
    assert {m.attempts for m in first + second} == {1}


async def test_claims_are_best_first_with_details(db: Database):
    company = await _company(db, "Cohere")
    await _postings(db, company, {"low": 0.5, "high": 0.8, "mid": 0.6})
    claimed = await claim_new_matches(db, RadarConfig())
    assert [m.title for m in claimed] == ["High ML Intern", "Mid ML Intern", "Low ML Intern"]
    assert claimed[0].company == "Cohere"
    assert claimed[0].terms == ["Summer 2027", "Fall 2027"] and claimed[0].locations == ["Toronto, ON"]
    assert claimed[0].url == "https://example.com/high" and claimed[0].attempts == 0


# ---- pipeline and watcher wiring --------------------------------------------------------------------


async def test_run_pipeline_claims_nothing_unless_asked(db: Database):
    company = await _company(db)
    await _postings(db, company, {"best": 0.9})
    async with make_client(FakeBoards()) as client:
        summary = await run_pipeline(db, None, client, RadarConfig())
        assert summary.notify is None and summary.events == []
        assert await _notifications(db) == {}

        summary = await run_pipeline(db, None, client, RadarConfig(), notify=True)
    assert summary.ok and summary.notify.new == 1 and len(summary.events) == 1
    assert "notify: claimed 1 new, 0 retries; 1 messages" in describe(summary)


async def test_notify_stage_failure_is_isolated(db: Database, monkeypatch):
    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(notify, "notify_matches", boom)
    async with make_client(FakeBoards()) as client:
        summary = await run_pipeline(db, None, client, RadarConfig(), notify=True)
    assert summary.errors == {"notify": "RuntimeError: db hiccup"} and summary.events == []


async def test_on_pushed_error_is_logged_and_other_events_still_push(radar: Harness, monkeypatch):
    company = await _company(radar.db)
    await _postings(radar.db, company, {"a": 0.9, "b": 0.8})

    async def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("cannot record")

    monkeypatch.setattr("orbit.radar.watcher.record_delivery", broken)
    assert len(await radar.poll()) == 2  # both pushed; rows stay pending and later expire (never re-sent)
    assert {n.status for n in (await _notifications(radar.db)).values()} == {"pending"}
    assert await radar.poll() == []


# ---- 8. other watchers ------------------------------------------------------------------------------


class PlainWatcher(Watcher):
    """Defines only check: inherits the no-op on_pushed."""

    name = "plain_dummy"

    async def check(self, db: Database) -> list[Event]:
        return [Event(title="one", body="first"), Event(title="two")]


class DuckWatcher:
    """Not a Watcher subclass at all: no bind/close/on_pushed."""

    name = "duck_dummy"

    async def check(self, db: Database) -> list[Event]:
        return [Event(title="quack")]


class RecordingWatcher(PlainWatcher):
    name = "recording_dummy"

    def __init__(self) -> None:
        self.results: list[tuple[str, bool]] = []

    async def on_pushed(self, event: Event, ok: bool) -> None:
        self.results.append((event.title, ok))


async def test_other_watchers_still_work(tmp_path: Path, db: Database):
    app = make_app(tmp_path, db)
    channel = FakeTelegram()
    app.channels["telegram"] = channel

    await app.scheduler._poll_watcher(PlainWatcher())
    await app.scheduler._poll_watcher(DuckWatcher())  # type: ignore[arg-type]
    assert channel.sent == ["[plain_dummy] one\nfirst", "[plain_dummy] two", "[duck_dummy] quack"]

    recording = RecordingWatcher()
    channel.failing = True
    await app.scheduler._poll_watcher(recording)
    assert recording.results == [("one", False), ("two", False)]
    channel.failing = False
    await app.scheduler._poll_watcher(recording)
    assert recording.results[2:] == [("one", True), ("two", True)]
