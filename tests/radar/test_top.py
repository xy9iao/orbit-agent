"""`orbit radar top` (M5.5) through CliRunner on the test database.

`top` rescores before listing, so postings are seeded with crafted embeddings and a resume vector whose
cosine similarities are known exactly (see test_match's scoring tests), not with preset scores.
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select
from typer.testing import CliRunner

import orbit.radar.cli as radar_cli
from orbit.cli.main import app
from orbit.radar.match import ScoreCounts, TopMatch
from orbit.storage.db import Database
from orbit.storage.models import Notification, Posting
from tests.radar.test_match import NOT_INTERNSHIP, _add, _company, _profile, _row, _scorable, vec
from tests.radar.test_pipeline import _patch_runtime
from tests.radar.test_seed import sql

# external_id -> cosine similarity to the resume vec(1.0)
SIMS = {"best": 0.9, "good": 0.7, "ok": 0.5, "weak": 0.3}


def _v(similarity: float) -> list[float]:
    return vec(similarity, (1 - similarity**2) ** 0.5)


def _seed(database_url: str, *, resume: bool = True, embedded: bool = True, extra: list[Any] | None = None) -> None:
    async def _go() -> None:
        await sql(database_url, "TRUNCATE radar_companies, radar_profile RESTART IDENTITY CASCADE")
        database = Database(database_url)
        await database.connect()
        try:
            company = await _company(database)
            if resume:
                await _profile(database, "resume", vec(1.0))
            if embedded:
                rows = [_scorable(company, key, _v(sim), title=f"{key.title()} ML Intern") for key, sim in SIMS.items()]
                rows += [
                    _scorable(company, "fulltime", _v(0.95), extraction=NOT_INTERNSHIP),
                    _scorable(company, "closed", _v(0.99), status="closed"),
                ]
            else:
                rows = [_row(company, key) for key in SIMS]
            await _add(database, *rows, *(fn(company) for fn in extra or []))
        finally:
            await database.close()

    asyncio.run(_go())


def _notify(database_url: str, external_id: str, status: str = "sent") -> None:
    async def _go() -> None:
        database = Database(database_url)
        await database.connect()
        try:
            async with database.session() as s:
                posting_id = await s.scalar(select(Posting.id).where(Posting.external_id == external_id))
                s.add(Notification(posting_id=posting_id, score=0.9, status=status))
                await s.commit()
        finally:
            await database.close()

    asyncio.run(_go())


def _top(monkeypatch, tmp_path: Path, database_url: str, *args: str, **radar: Any) -> str:
    _patch_runtime(monkeypatch, database_url, tmp_path, {}, **radar)  # no providers: top needs no API key
    result = CliRunner().invoke(app, ["radar", "top", *args])
    assert result.exit_code == 0, result.output
    return result.output


def _table(output: str) -> list[str]:
    """Lines between the header row and the footer: data rows and the threshold marker."""
    lines = output.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.split()[:3] == ["#", "score", "company"])
    end = next(i for i, ln in enumerate(lines) if "open postings scored" in ln)
    return lines[start + 1 : end]


def _ids(table: list[str]) -> list[str]:
    """Row order as external ids ("Best ML Intern" -> "best"), with "|" for the marker line."""
    out = []
    for ln in table:
        if ln.startswith("----"):
            out.append("|")
        else:
            out.append(next(w for w in ln.split() if w.lower() in SIMS).lower())
    return out


def test_top_lists_open_scored_postings_best_first_with_the_threshold_marker(monkeypatch, tmp_path, database_url):
    _seed(database_url)

    output = _top(monkeypatch, tmp_path, database_url)

    assert "top 4 open matches by cosine similarity to your resume:" in output
    table = _table(output)
    assert _ids(table) == ["best", "good", "ok", "|", "weak"]  # fulltime (filtered) and closed are absent
    assert table[3] == "---- notify_threshold 0.450: rows above this line qualify for a Telegram push ----"
    first = table[0]
    for column in ("1", "0.900", "Acme AI", "Best ML Intern", "Summer 2027, Fall 2027", "Toronto, ON",
                   "https://example.com/best"):
        assert column in first
    assert first.split()[:2] == ["1", "0.900"]
    assert [ln.split()[1] for ln in table if not ln.startswith("----")] == ["0.900", "0.700", "0.500", "0.300"]
    assert "4 open postings scored; 3 at or above notify_threshold 0.450" in output


def test_top_n_limit_and_threshold_override(monkeypatch, tmp_path, database_url):
    _seed(database_url)

    two = _table(_top(monkeypatch, tmp_path, database_url, "2"))
    assert _ids(two) == ["best", "good", "|"]  # every shown row is above the cut: marker at the end

    custom = _top(monkeypatch, tmp_path, database_url, "--threshold", "0.8")
    assert _ids(_table(custom)) == ["best", "|", "good", "ok", "weak"]
    assert "4 open postings scored; 1 at or above notify_threshold 0.800" in custom

    from_config = _top(monkeypatch, tmp_path, database_url, notify_threshold=0.95)
    assert _ids(_table(from_config)) == ["|", "best", "good", "ok", "weak"]


def test_top_marks_notified_postings_and_new_hides_them(monkeypatch, tmp_path, database_url):
    _seed(database_url)
    _notify(database_url, "best", "sent")
    _notify(database_url, "ok", "failed")

    output = _top(monkeypatch, tmp_path, database_url)
    table = _table(output)
    assert "Best ML Intern [sent]" in table[0] and "Ok ML Intern [failed]" in table[2]

    new = _table(_top(monkeypatch, tmp_path, database_url, "--new"))
    assert _ids(new) == ["good", "|", "weak"]


def test_top_rescores_with_the_current_filters(monkeypatch, tmp_path, database_url):
    _seed(database_url)
    _top(monkeypatch, tmp_path, database_url)

    output = _top(monkeypatch, tmp_path, database_url, terms=["Winter 2028"])  # nothing matches any more

    assert "no scored postings: none of the 4 embedded internship postings passes the rule filters" in output
    assert asyncio.run(sql(database_url, "SELECT count(*) FROM radar_postings WHERE score IS NOT NULL")) == [(0,)]


def test_top_empty_hints(monkeypatch, tmp_path, database_url):
    asyncio.run(sql(database_url, "TRUNCATE radar_companies, radar_profile RESTART IDENTITY CASCADE"))
    assert "no open postings yet: run `orbit radar seed`" in _top(monkeypatch, tmp_path, database_url)

    _seed(database_url, resume=False)
    assert "no resume embedded yet: run `orbit radar resume`" in _top(monkeypatch, tmp_path, database_url)

    _seed(database_url, embedded=False)
    output = _top(monkeypatch, tmp_path, database_url)
    assert "4 internship postings have no current embedding: run `orbit radar embed`" in output

    _seed(database_url)
    _notify(database_url, "best")
    for key in ("good", "ok", "weak"):
        _notify(database_url, key)
    assert "all 4 scored postings were already notified (drop --new)" in _top(
        monkeypatch, tmp_path, database_url, "--new"
    )


def test_top_hint_for_unextracted_postings(monkeypatch, tmp_path, database_url):
    pending = [lambda c: _row(c, "pending", extraction_status="pending", extraction=None)]
    _seed(database_url, embedded=False, extra=pending)
    asyncio.run(sql(database_url, "DELETE FROM radar_postings WHERE external_id <> 'pending'"))

    output = _top(monkeypatch, tmp_path, database_url)

    assert "1 open postings are not extracted yet: run `orbit radar extract`" in output


def test_top_output_survives_a_non_utf8_console(monkeypatch):
    match = TopMatch(0.8123, "Montréal AI", "Développeur → Stage", ["Fall 2027"], ["Montreal, QC"],
                     "https://example.com/x", None)
    text = radar_cli.format_top([match], 0.45, ScoreCounts(1, 1))
    assert "0.812" in text and "Développeur → Stage" in text

    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    printable = radar_cli._printable(text)
    printable.encode("cp1252")  # no UnicodeEncodeError
    assert "Développeur ? Stage" in printable  # é exists in cp1252, the arrow does not


def test_format_top_clips_long_titles_and_marks_missing_fields():
    long = TopMatch(0.5, "Acme", "x" * 200, [], [], "https://example.com/long", "pending")
    [_, row, *_] = radar_cli.format_top([long], 0.45, ScoreCounts(1, 1)).splitlines()
    assert ("x" * (radar_cli.TOP_TITLE_WIDTH - 3) + "... [pending]") in row
    assert row.count("not stated") == 2
