"""`orbit radar stats` (M6.3): every metric against hand-computed values on the test database."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

import orbit.radar.cli as radar_cli
from orbit.cli.main import app
from orbit.radar.eval import KV_LAST_EVAL, UNVERIFIED_BANNER
from orbit.radar.stats import (
    FieldAccuracy,
    SourceCount,
    TaskUsage,
    collect_stats,
    format_duration,
    format_stats,
)
from orbit.storage.db import Database
from orbit.storage.models import Base, Company, Notification, Run, Usage
from tests.radar.test_match import NOT_INTERNSHIP, SKIPPED, _row
from tests.radar.test_pipeline import _patch_runtime
from tests.radar.test_seed import sql

NOW = datetime(2026, 10, 15, 12, 0, 0)

LAST_EVAL = {
    "at": "2026-10-14T20:15:00",
    "model": "gpt-5-mini",
    "label_file": "evals/radar_labels.yaml",
    "unverified": True,
    "n": 20,
    "failures": 1,
    "fields": {
        "is_internship": {"accuracy": 0.95, "n": 20},
        "terms": {"accuracy": 0.8, "n": 19, "mean_jaccard": 0.9123},
        "role_type": {"accuracy": 0.75, "n": 19},
    },
}

# external_id -> (status, extraction_status, extraction, embedded_hash, score)
POSTINGS = {
    "a": ("open", "ok", None, "current", 0.8),
    "b": ("open", "ok", None, "old", None),  # stale embedding
    "c": ("closed", "ok", None, "current", 0.7),  # closed: its score is history, not "scored"
    "d": ("open", "ok", NOT_INTERNSHIP, None, None),
    "e": ("open", "failed", "none", None, None),
    "f": ("open", "skipped", SKIPPED, None, None),
    "g": ("closed", "pending", "none", None, None),
    "h": ("open", "pending", "none", None, None),
}
# external_id -> (notification status, seconds from first_seen_at to sent_at or None)
NOTIFICATIONS = {
    "a": ("sent", 60),
    "b": ("sent", 120),
    "c": ("sent", 300),
    "d": ("sent", 600),
    "e": ("sent", 3600),
    "f": ("failed", 99_999),  # a sent_at on a failed row must not count
    "g": ("pending", None),
}
# (started_at, ok)
RUNS = [
    (datetime(2026, 10, 10, 10, 0), True),
    (datetime(2026, 10, 10, 18, 0), True),  # same day: still one uptime day
    (datetime(2026, 10, 12, 23, 59), True),
    (datetime(2026, 10, 13, 9, 0), False),  # failed only: not an uptime day
    (datetime(2026, 10, 14, 9, 0), None),  # unfinished (stale NULL)
    (datetime(2026, 10, 14, 11, 0), True),  # 25 h before NOW: outside the 24 h window
    (datetime(2026, 10, 14, 13, 0), True),
    (datetime(2026, 10, 15, 1, 0), True),
    (datetime(2026, 10, 15, 11, 0), False),
    (datetime(2026, 10, 15, 11, 30), None),  # NULL inside the window counts as neither ok nor failed
]
# (task, created_at, prompt, completion)
USAGE = [
    ("extract", datetime(2026, 10, 15, 1, 0), 1000, 200),
    ("extract", datetime(2026, 10, 15, 9, 0), 1500, 300),
    ("extract", datetime(2026, 10, 15, 11, 59), 500, 100),
    ("embed", datetime(2026, 10, 15, 2, 0), 4000, 0),
    ("extract", datetime(2026, 10, 14, 23, 59), 9999, 9999),  # yesterday
    ("chat", datetime(2026, 10, 15, 10, 0), 7777, 7777),  # not a radar task
]


async def _seed(db: Database, *, last_eval: dict | None = LAST_EVAL) -> None:
    async with db.session() as s:
        acme = Company(slug="acme", name="Acme", source="greenhouse", board_token="acme", last_verify_status="ok")
        companies = [
            acme,
            Company(slug="beta", name="Beta", source="lever", board_token="beta", last_verify_status="error"),
            Company(slug="gamma", name="Gamma", source="ashby", board_token="gamma"),
            # Inactive: counted in the total, never as tracked or verified.
            Company(slug="dormant", name="Dormant", source="greenhouse", board_token="dormant", active=False,
                    last_verify_status="ok"),
        ]
        s.add_all(companies)
        await s.flush()

        postings = {}
        for i, (ext, (status, ex_status, extraction, embedded, score)) in enumerate(POSTINGS.items()):
            kw = {"status": status, "extraction_status": ex_status, "score": score,
                  "first_seen_at": NOW - timedelta(days=2, hours=i)}
            if extraction == "none":
                kw["extraction"] = None
            elif extraction is not None:
                kw["extraction"] = extraction
            if embedded == "current":
                kw["embedded_hash"] = f"hash-{ext}"
            elif embedded is not None:
                kw["embedded_hash"] = embedded
            postings[ext] = _row(acme, ext, **kw)
        s.add_all(postings.values())
        await s.flush()

        for ext, (status, latency) in NOTIFICATIONS.items():
            p = postings[ext]
            sent_at = p.first_seen_at + timedelta(seconds=latency) if latency is not None else None
            s.add(Notification(posting_id=p.id, score=0.9, status=status, sent_at=sent_at, attempts=1))
        for started_at, ok in RUNS:
            s.add(Run(company_id=acme.id, started_at=started_at, finished_at=started_at, ok=ok))
        for task, created_at, prompt, completion in USAGE:
            s.add(Usage(task=task, provider="openai", model="m", prompt_tokens=prompt,
                        completion_tokens=completion, created_at=created_at))
        await s.commit()
    if last_eval is not None:
        await db.kv_set(KV_LAST_EVAL, last_eval)


# ---------------------------------------------------------------------------
# collect_stats
# ---------------------------------------------------------------------------


async def test_collect_stats_counts_every_metric(db: Database):
    await _seed(db)

    stats = await collect_stats(db, now=NOW)

    c = stats.companies
    assert (c.total, c.active, c.verified_ok) == (4, 3, 1)
    assert c.by_source == {
        "greenhouse": SourceCount(total=2, active=1),
        "lever": SourceCount(total=1, active=1),
        "ashby": SourceCount(total=1, active=1),
    }

    p = stats.postings
    assert (p.total, p.open, p.closed) == (8, 6, 2)
    assert p.extraction == {"ok": 4, "failed": 1, "skipped": 1, "pending": 2}
    assert p.internships == 3  # a, b, c (d is not an internship; skipped rows store is_internship false)
    assert p.embedded == 2  # a, c (b is stale)
    assert p.scored == 1  # a (c is closed)

    ev = stats.eval
    assert ev is not None
    assert (ev.at, ev.model, ev.n, ev.failures, ev.unverified) == ("2026-10-14T20:15:00", "gpt-5-mini", 20, 1, True)
    assert ev.label_file == "evals/radar_labels.yaml"
    assert ev.fields == [
        FieldAccuracy("is_internship", 0.95, 20, None),
        FieldAccuracy("terms", 0.8, 19, 0.9123),
        FieldAccuracy("role_type", 0.75, 19, None),
    ]

    lat = stats.latency
    assert (lat.sent, lat.failed, lat.pending) == (5, 1, 1)
    assert lat.median_seconds == 300.0  # of 60, 120, 300, 600, 3600
    assert lat.p90_seconds == 2400.0  # percentile_cont: 600 + 0.6 * (3600 - 600)

    up = stats.uptime
    assert up.days == 4  # Oct 10, 12, 14, 15; Oct 13 had only a failed run, NULL rows never count
    assert up.first_ok_at == datetime(2026, 10, 10, 10, 0)
    assert up.last_ok_at == datetime(2026, 10, 15, 1, 0)
    assert (up.runs_24h_ok, up.runs_24h_failed) == (2, 1)

    assert stats.usage_today == [TaskUsage("extract", 3, 3000, 600), TaskUsage("embed", 1, 4000, 0)]
    assert stats.generated_at == NOW


async def test_collect_stats_on_an_empty_database(db: Database):
    stats = await collect_stats(db, now=NOW)

    assert (stats.companies.total, stats.companies.active, stats.companies.verified_ok) == (0, 0, 0)
    assert all(sc == SourceCount() for sc in stats.companies.by_source.values())
    assert stats.postings.total == 0
    assert stats.postings.extraction == {"ok": 0, "failed": 0, "skipped": 0, "pending": 0}
    assert stats.eval is None
    assert stats.latency.median_seconds is None and stats.latency.p90_seconds is None
    assert stats.uptime.days == 0 and stats.uptime.first_ok_at is None
    assert stats.usage_today == [TaskUsage("extract", 0, 0, 0), TaskUsage("embed", 0, 0, 0)]


async def test_single_sent_notification_has_equal_median_and_p90(db: Database):
    async with db.session() as s:
        company = Company(slug="acme", name="Acme", source="greenhouse", board_token="acme")
        s.add(company)
        await s.flush()
        posting = _row(company, "x", first_seen_at=NOW - timedelta(hours=2))
        s.add(posting)
        await s.flush()
        s.add(Notification(posting_id=posting.id, score=0.9, status="sent", sent_at=NOW - timedelta(hours=1)))
        await s.commit()

    lat = (await collect_stats(db, now=NOW)).latency

    assert (lat.median_seconds, lat.p90_seconds, lat.sent) == (3600.0, 3600.0, 1)


async def test_malformed_eval_summary_does_not_crash_and_counts_as_unverified(db: Database):
    await db.kv_set(KV_LAST_EVAL, {"n": 3, "fields": {"terms": {"accuracy": 0.5, "n": 3}, "bad": "x"}})

    ev = (await collect_stats(db, now=NOW)).eval

    assert ev is not None and ev.unverified is True  # missing flag: provisional
    assert ev.fields == [FieldAccuracy("terms", 0.5, 3, None)]

    await db.kv_set(KV_LAST_EVAL, "garbage")
    assert (await collect_stats(db, now=NOW)).eval is None


# ---------------------------------------------------------------------------
# format_stats
# ---------------------------------------------------------------------------


async def test_format_stats_text(db: Database):
    await _seed(db)

    text = format_stats(await collect_stats(db, now=NOW))
    lines = text.splitlines()

    assert lines[0] == "Internship Radar stats (2026-10-15 12:00)"
    assert "  tracked: 3 active of 4; 1 active with a passing verify-sources check" in lines
    assert "  by source (active/total): greenhouse 1/2, lever 1/1, ashby 1/1" in lines
    assert "  stored: 8 (6 open, 2 closed)" in lines
    assert "  extraction: 4 ok, 1 failed, 1 skipped (prefilter), 2 pending" in lines
    assert "  internships: 3; embedded (current): 2; open and scored: 1" in lines
    assert "  run 2026-10-14T20:15:00, model gpt-5-mini, 20 items, 1 failed extractions" in lines
    assert "  labels: evals/radar_labels.yaml" in lines
    assert "  is_internship        95.0%        -   20" in lines
    assert "  terms                80.0%     0.91   19" in lines
    assert "  role_type            75.0%        -   19" in lines
    assert "  median 5m 00s, p90 40m 00s over 5 sent" in lines
    assert "  notifications: 5 sent, 1 failed, 1 pending" in lines
    assert "  4 days with at least one successful poll" in lines
    assert "  first successful poll: 2026-10-10 10:00; last: 2026-10-15 01:00" in lines
    assert "  company polls in the last 24 h: 2 ok, 1 failed" in lines
    assert "  extract: 3 calls, 3000 prompt + 600 completion tokens" in lines
    assert "  embed: 1 calls, 4000 prompt + 0 completion tokens" in lines
    # The provisional warning sits in the accuracy section and closes the report.
    assert text.count(UNVERIFIED_BANNER) == 2
    assert lines[-1] == UNVERIFIED_BANNER


async def test_format_stats_verified_eval_has_no_warning(db: Database):
    await _seed(db, last_eval={**LAST_EVAL, "unverified": False})

    text = format_stats(await collect_stats(db, now=NOW))

    assert "UNVERIFIED" not in text
    assert "  terms                80.0%     0.91   19" in text.splitlines()


async def test_format_stats_empty_database(db: Database):
    text = format_stats(await collect_stats(db, now=NOW))
    lines = text.splitlines()

    assert "  tracked: 0 active of 0; 0 active with a passing verify-sources check" in lines
    assert "  stored: 0 (0 open, 0 closed)" in lines
    assert "  no eval run yet (run `orbit radar eval`)" in lines
    assert "  none sent yet" in lines
    assert "  notifications: 0 sent, 0 failed, 0 pending" in lines
    assert "  0 days with at least one successful poll" in lines
    assert "  first successful poll: never; last: never" in lines
    assert "UNVERIFIED" not in text


def test_format_duration():
    assert format_duration(0) == "0s"
    assert format_duration(44.6) == "45s"
    assert format_duration(725) == "12m 05s"
    assert format_duration(3 * 3600 + 7 * 60 + 30) == "3h 07m"
    assert format_duration(2 * 86400 + 4 * 3600 + 59) == "2d 04h"


async def test_formatted_stats_survive_a_non_utf8_console(db: Database, monkeypatch):
    await _seed(db)
    text = format_stats(await collect_stats(db, now=NOW))

    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="gbk"))
    printable = radar_cli._printable(text)
    printable.encode("gbk")  # no UnicodeEncodeError
    assert "UNVERIFIED LABELS" in printable


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _seed_sync(database_url: str) -> None:
    async def _go() -> None:
        tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        await sql(database_url, f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
        database = Database(database_url)
        await database.connect()
        try:
            await _seed(database)
        finally:
            await database.close()

    asyncio.run(_go())


def _stats(monkeypatch, tmp_path: Path, database_url: str, *args: str) -> str:
    _patch_runtime(monkeypatch, database_url, tmp_path, {})  # no providers: stats needs no API key
    result = CliRunner().invoke(app, ["radar", "stats", *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_cli_stats_text(monkeypatch, tmp_path, database_url):
    _seed_sync(database_url)

    output = _stats(monkeypatch, tmp_path, database_url)

    # The command uses the real clock, so only time-independent lines are asserted here.
    assert output.startswith("Internship Radar stats (")
    assert "  stored: 8 (6 open, 2 closed)" in output
    assert "  median 5m 00s, p90 40m 00s over 5 sent" in output
    assert "  terms                80.0%     0.91   19" in output
    assert "UNVERIFIED LABELS" in output
    assert "days with at least one successful poll" in output


def test_cli_stats_json(monkeypatch, tmp_path, database_url):
    _seed_sync(database_url)

    data = json.loads(_stats(monkeypatch, tmp_path, database_url, "--json"))

    assert data["companies"] == {
        "total": 4,
        "active": 3,
        "verified_ok": 1,
        "by_source": {
            "greenhouse": {"total": 2, "active": 1},
            "lever": {"total": 1, "active": 1},
            "ashby": {"total": 1, "active": 1},
        },
    }
    assert data["postings"]["extraction"] == {"ok": 4, "failed": 1, "skipped": 1, "pending": 2}
    assert data["latency"] == {"sent": 5, "failed": 1, "pending": 1, "median_seconds": 300.0, "p90_seconds": 2400.0}
    assert data["eval"]["unverified"] is True
    assert data["eval"]["fields"][1] == {"name": "terms", "accuracy": 0.8, "n": 19, "mean_jaccard": 0.9123}
    assert data["uptime"]["days"] == 4
    assert data["uptime"]["first_ok_at"] == "2026-10-10T10:00:00"
    datetime.fromisoformat(data["generated_at"])


def test_cli_stats_empty_database(monkeypatch, tmp_path, database_url):
    asyncio.run(sql(database_url, "TRUNCATE radar_companies, kv, radar_runs RESTART IDENTITY CASCADE"))

    output = _stats(monkeypatch, tmp_path, database_url)

    assert "no eval run yet" in output
    assert "none sent yet" in output
    assert "  0 days with at least one successful poll" in output
