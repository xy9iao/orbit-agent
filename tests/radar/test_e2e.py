"""Internship Radar end to end (M6.4): recorded boards -> poll -> extract -> embed -> score -> Telegram push.

Everything between the network edges is real: OrbitApp, OrbitScheduler (start() registers the
`internship_radar` watcher from config, `_poll_watcher` runs it and pushes through `OrbitApp.push`), the
pipeline stages, ModelRouter and the Postgres/pgvector `db` fixture database. Faked are only the edges:
- job boards: the three recorded fixtures (Greenhouse Faire, Lever PointClickCare, Ashby Cohere) served by
  an httpx.MockTransport through the watcher's own PoliteClient (injected with the `client_factory` key of
  the watcher's config entry); the autouse `no_network` guard fails any request that escapes it;
- the LLM: `RadarLLM.complete` returns a scripted, valid PostingExtraction JSON per posting title, and its
  `embed` is conftest's deterministic `fake_embedding` (sha256-seeded unit vectors);
- Telegram: test_notify's FakeTelegram, which only records texts.

How the scores are made predictable: fake embeddings of different texts are nearly orthogonal (cosine
about 0 +- 0.03 in 1536 dims). The resume profile row is written directly with the embedding
`unit(fake_embedding(A) + fake_embedding(B))`, where A and B are the exact `match.embedding_text` of the
two target postings (Cohere's "Software Engineer Intern" and "Machine Learning Intern/Co-op"), computed up
front from the same fixture, adapter and scripted extraction. Those two then score about 0.71 (above the
0.5 threshold) and every other posting about 0, and the exact expected score is computed in Python.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from orbit.app import OrbitApp
from orbit.channels.base import Incoming
from orbit.config import RadarConfig
from orbit.core.providers.base import Response
from orbit.core.router import ModelRouter
from orbit.radar.extract import PREFILTER_MODEL, parse_extraction
from orbit.radar.match import embedding_text
from orbit.radar.profile import text_hash
from orbit.radar.sources import get_adapter
from orbit.radar.watcher import InternshipRadarWatcher
from orbit.storage.db import Database
from orbit.storage.models import Company, Notification, Posting, Profile, Run
from tests.conftest import FakeProvider, fake_embedding, tool_call_response
from tests.radar.test_notify import FakeTelegram
from tests.radar.test_poll import FIXTURES, make_client, no_network  # noqa: F401 - autouse network guard
from tests.test_health import make_app

PREFIX = "[internship_radar] "

# (source, board token) -> (fixture file, company name). One company per source adapter.
BOARDS = {
    ("greenhouse", "faire"): ("greenhouse_faire.json", "Faire"),
    ("lever", "pointclickcare"): ("lever_pointclickcare.json", "PointClickCare"),
    ("ashby", "cohere"): ("ashby_cohere.json", "Cohere"),
}
HOSTS = {"boards-api.greenhouse.io": "greenhouse", "api.lever.co": "lever", "api.ashbyhq.com": "ashby"}

SWE_INTERN = "Software Engineer Intern (Winter 2027)"
ML_INTERN = "Machine Learning Intern/Co-op  (Winter 2027)"  # the double space is in the Ashby fixture
RESEARCH_INTERN = "Research Internship (Winter 2027)"
FAIRE_INTERN = "Applied AI/ML Scientist, Intern"
TARGETS = (SWE_INTERN, ML_INTERN)  # the resume is built to match these two (module docstring)
ML_INTERN_ID = "36d1f52f-8270-4652-adf5-5303a0ff341b"

# Titles the prefilter skips without an LLM call (seniority word, nothing student-like).
PREFILTERED = {
    "Director, Global Equity",
    "(Canada) Principal ML System Engineer",
    "(US) Principal ML System Engineer",
    "Senior Full-Stack Engineer, Internal Applications",
    "Member of Technical Staff, Modeling",
}


def _cohere_intern(role_type: str, tech: list[str]) -> dict[str, Any]:
    return {
        "is_internship": True,
        "terms": ["Winter 2027"],
        "locations": ["Remote (Canada)"],
        "remote_ok": True,
        "canada_eligible": True,
        "role_type": role_type,
        "tech_stack": tech,
    }


def _not_intern(locations: list[str], *, remote: bool, canada: bool, role_type: str = "other") -> dict[str, Any]:
    return {
        "is_internship": False,
        "terms": [],
        "locations": locations,
        "remote_ok": remote,
        "canada_eligible": canada,
        "role_type": role_type,
        "tech_stack": [],
    }


# What the fake LLM answers per posting title: valid PostingExtraction JSON, close to what a real model
# would read from each fixture posting. Faire's intern role is real but in San Francisco, so it is
# extracted and embedded yet fails the location rule (never scored); Cohere's three interns are remote in
# Canada for Winter 2027 and pass every rule.
SCRIPT: dict[str, dict[str, Any]] = {
    FAIRE_INTERN: {
        "is_internship": True,
        "terms": [],
        "locations": ["San Francisco, CA"],
        "remote_ok": False,
        "canada_eligible": False,
        "role_type": "ml_ai",
        "tech_stack": ["python"],
    },
    "Account Executive": _not_intern(["New York, NY", "San Francisco, CA"], remote=False, canada=False),
    "Account Executive Bilingual (Spanish/English)": _not_intern(["London, UK"], remote=False, canada=False),
    "Customer Experience Associate (3 Month Fixed-Term Contract)": _not_intern(
        ["Kitchener, ON"], remote=False, canada=True
    ),
    "(Canada) Sales Development Representative": _not_intern(["Mississauga, ON"], remote=True, canada=True),
    "(Canada) Sales Development Representative (6 month contract)": _not_intern(
        ["Mississauga, ON"], remote=True, canada=True
    ),
    "(Canada) Software Implementation Consultant - Clinical": _not_intern(
        ["Mississauga, ON"], remote=True, canada=True
    ),
    SWE_INTERN: _cohere_intern("swe", ["python", "go", "typescript"]),
    RESEARCH_INTERN: _cohere_intern("research", ["python", "pytorch"]),
    ML_INTERN: _cohere_intern("ml_ai", ["python", "pytorch", "jax"]),
}
INTERNS = {title for title, fields in SCRIPT.items() if fields["is_internship"]}


def load_payload(source: str, token: str) -> Any:
    return json.loads((FIXTURES / BOARDS[(source, token)][0]).read_text(encoding="utf-8"))


class Boards:
    """Serves the three recorded boards by host and token (an httpx.MockTransport handler)."""

    def __init__(self) -> None:
        self.payloads = {key: load_payload(*key) for key in BOARDS}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        source = HOSTS[request.url.host]
        # greenhouse /v1/boards/{token}/jobs, lever /v0/postings/{token}, ashby /posting-api/job-board/{token}
        parts = request.url.path.strip("/").split("/")
        token = parts[2]
        payload = self.payloads.get((source, token))
        if payload is None:
            return httpx.Response(404, json={"error": "unknown board"})
        return httpx.Response(200, json=payload)

    def _jobs(self, source: str, token: str) -> list[dict[str, Any]]:
        payload = self.payloads[(source, token)]
        return payload if isinstance(payload, list) else payload["jobs"]

    def remove(self, source: str, token: str, external_id: str) -> tuple[int, dict[str, Any]]:
        jobs = self._jobs(source, token)
        [index] = [i for i, job in enumerate(jobs) if str(job["id"]) == external_id]
        return index, jobs.pop(index)

    def restore(self, source: str, token: str, removed: tuple[int, dict[str, Any]]) -> None:
        index, job = removed
        self._jobs(source, token).insert(index, job)


class RadarLLM(FakeProvider):
    """The `cheap` provider behind the extract and embed tasks.

    complete: answers the scripted extraction for the posting's title (extraction runs a few postings
    concurrently, so replies are keyed by title, not by call order); an unscripted title raises, which the
    pipeline records as a failed extraction and the status assertions catch. embed: FakeProvider's
    deterministic fake_embedding.
    """

    def __init__(self) -> None:
        super().__init__("cheap")
        self.extract_titles: list[str] = []

    async def complete(self, model, messages, tools=None, **params):
        self.calls.append({"model": model, "messages": messages, "tools": tools, "params": params})
        assert params.get("response_format", {}).get("type") == "json_schema"
        title = re.search(r"^Title: (.*)$", messages[1]["content"], re.MULTILINE).group(1)
        self.extract_titles.append(title)
        return Response(text=json.dumps(SCRIPT[title]), usage={"prompt_tokens": 100, "completion_tokens": 20})


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def expected_embedding_texts(titles: tuple[str, ...]) -> dict[str, str]:
    """`match.embedding_text` of the Cohere postings with these titles, as the embed stage will build it:
    the posting as the Ashby adapter normalizes the fixture, plus the scripted extraction after validation."""
    async with make_client(Boards()) as client:
        company = SimpleNamespace(id=0, slug="cohere", source="ashby", board_token="cohere")
        postings = {p.title: p for p in await get_adapter("ashby").fetch(client, company)}
    out = {}
    for title in titles:
        p = postings[title]
        extraction = parse_extraction(json.dumps(SCRIPT[title])).model_dump(mode="json")
        row = SimpleNamespace(
            title=p.title, description_text=p.description_text, extraction_status="ok", extraction=extraction
        )
        out[title] = embedding_text(row, "Cohere")
    return out


class E2E:
    def __init__(self, app: OrbitApp, db: Database, boards: Boards, llm: RadarLLM, chat: FakeProvider,
                 channel: FakeTelegram, watcher: InternshipRadarWatcher, resume: list[float],
                 target_texts: dict[str, str]):
        self.app, self.db, self.boards, self.llm, self.chat = app, db, boards, llm, chat
        self.channel, self.watcher, self.resume, self.target_texts = channel, watcher, resume, target_texts

    async def run(self) -> list[str]:
        """One scheduled radar run through the real scheduler path; returns the texts it delivered."""
        before = len(self.channel.sent)
        await self.app.scheduler._poll_watcher(self.watcher)
        return self.channel.sent[before:]

    async def postings(self) -> dict[str, Posting]:
        """Every stored posting by title (titles are unique across the three fixtures)."""
        async with self.db.session() as s:
            rows = list((await s.execute(select(Posting))).scalars())
        by_title = {p.title: p for p in rows}
        assert len(by_title) == len(rows)
        return by_title

    async def notifications(self) -> dict[int, Notification]:
        async with self.db.session() as s:
            return {n.posting_id: n for n in (await s.execute(select(Notification))).scalars()}

    def expected_score(self, title: str) -> float:
        return _dot(fake_embedding(self.target_texts[title]), self.resume)


@pytest.fixture
async def e2e(tmp_path: Path, db: Database):
    boards = Boards()
    app = make_app(tmp_path, db)
    app.config.jobs.briefing.enabled = False
    app.config.radar = RadarConfig(
        terms=["Winter 2027"],
        locations=["Toronto", "Waterloo", "Remote (Canada)"],
        notify_threshold=0.5,
        max_extract_per_poll=40,
        max_notifications_per_poll=5,
    )
    # The real _register_watcher builds the watcher from this entry: every key besides type/interval goes to
    # the constructor, so `client_factory` hands the watcher's PoliteClient the recorded boards (no sleeping).
    app.config.watchers = [
        {"type": "internship_radar", "interval_minutes": 30, "client_factory": lambda: make_client(boards)}
    ]

    llm = RadarLLM()
    chat = FakeProvider("openai")
    app.router = ModelRouter(app.config, {"openai": chat, "cheap": llm}, usage_sink=app._record_usage)
    app.agent.router = app.router
    channel = FakeTelegram()
    app.channels["telegram"] = channel

    async with db.session() as s:
        s.add_all(
            Company(slug=token, name=name, source=source, board_token=token, active=True)
            for (source, token), (_file, name) in BOARDS.items()
        )
        await s.commit()

    # The resume trick from the module docstring: equal parts of the two target postings' embeddings.
    target_texts = await expected_embedding_texts(TARGETS)
    resume = _unit([a + b for a, b in zip(*(fake_embedding(t) for t in target_texts.values()), strict=True)])
    resume_text = "Stand-in resume: Python, PyTorch, Go; ML and backend co-ops."
    async with db.session() as s:
        s.add(Profile(kind="resume", text=resume_text, text_hash=text_hash(resume_text), embedding=resume))
        await s.commit()

    await app.scheduler.start()
    try:
        [watcher] = app.scheduler.watchers
        assert isinstance(watcher, InternshipRadarWatcher) and watcher.orbit is app
        yield E2E(app, db, boards, llm, chat, channel, watcher, resume, target_texts)
    finally:
        await app.scheduler.stop()


def _match_text(title: str, score: float, url: str) -> str:
    title = " ".join(title.split())  # the message collapses the fixture title's double space
    return f"{PREFIX}🎯 {score:.2f} Cohere — {title}\nTerms: Winter 2027 · Locations: Remote (Canada)\n{url}"


async def test_radar_end_to_end(e2e: E2E):
    # ---- run 1: everything is new ---------------------------------------------------------------------
    first = await e2e.run()

    assert sorted((HOSTS[r.url.host], r.url.path) for r in e2e.boards.requests) == [
        ("ashby", "/posting-api/job-board/cohere"),
        ("greenhouse", "/v1/boards/faire/jobs"),
        ("lever", "/v0/postings/pointclickcare"),
    ]
    rows = await e2e.postings()
    assert len(rows) == 15
    assert {p.source for p in rows.values()} == {"greenhouse", "lever", "ashby"}
    assert all(p.status == "open" for p in rows.values())

    # Extraction: one LLM call per non-prefiltered posting; the obvious senior titles never reach the LLM.
    assert set(rows) == set(SCRIPT) | PREFILTERED
    assert sorted(e2e.llm.extract_titles) == sorted(SCRIPT)
    for title, p in rows.items():
        if title in PREFILTERED:
            assert (p.extraction_status, p.extraction_model) == ("skipped", PREFILTER_MODEL), title
            assert p.extraction["is_internship"] is False
        else:
            assert (p.extraction_status, p.extraction_model, p.extraction_error) == ("ok", "extract-small", None)
            assert p.extraction == parse_extraction(json.dumps(SCRIPT[title])).model_dump(mode="json")

    # Embedding: only the extracted internships, in one batch; the resume trick's texts are what was embedded.
    [embed_call] = e2e.llm.embed_calls
    assert len(embed_call["texts"]) == len(INTERNS) == 4
    assert set(e2e.target_texts.values()) <= set(embed_call["texts"])
    for title, p in rows.items():
        if title in INTERNS:
            assert p.embedding is not None and p.embedded_hash == p.content_hash, title
        else:
            assert p.embedding is None and p.embedded_hash is None, title

    # Scoring: the two targets match the resume; Faire's intern fails the location rule (San Francisco) and
    # stays unscored; Cohere's research intern passes the rules but is unrelated to the resume vector.
    for title in TARGETS:
        assert rows[title].score == pytest.approx(e2e.expected_score(title), abs=1e-4)
        assert rows[title].score > 0.6
    assert abs(rows[RESEARCH_INTERN].score) < 0.2
    assert {t for t, p in rows.items() if p.score is not None} == {*TARGETS, RESEARCH_INTERN}

    # Notification: exactly one Telegram message per match, with score, company, title, terms and URL.
    assert sorted(first) == sorted(_match_text(t, rows[t].score, rows[t].url) for t in TARGETS)
    assert rows[ML_INTERN].url == f"https://jobs.ashbyhq.com/cohere/{ML_INTERN_ID}"
    sent = await e2e.notifications()
    assert set(sent) == {rows[t].id for t in TARGETS}
    for title in TARGETS:
        n = sent[rows[title].id]
        assert (n.status, n.attempts, n.error) == ("sent", 1, None)
        assert n.sent_at is not None and n.score == pytest.approx(rows[title].score)

    llm_calls, embed_calls = len(e2e.llm.calls), len(e2e.llm.embed_calls)
    extracted_at = {t: p.extracted_at for t, p in rows.items()}

    # ---- run 2: identical boards ----------------------------------------------------------------------
    assert await e2e.run() == []
    assert len(e2e.boards.requests) == 6
    assert (len(e2e.llm.calls), len(e2e.llm.embed_calls)) == (llm_calls, embed_calls)
    rows = await e2e.postings()
    assert {t: p.extracted_at for t, p in rows.items()} == extracted_at
    assert all(p.status == "open" for p in rows.values())
    assert {pid: (n.status, n.attempts) for pid, n in (await e2e.notifications()).items()} == {
        rows[t].id: ("sent", 1) for t in TARGETS
    }
    async with e2e.db.session() as s:
        runs = list((await s.execute(select(Run))).scalars())
    assert len(runs) == 6 and all(r.ok for r in runs)

    # ---- run 3: a notified posting disappears, then comes back ---------------------------------------
    removed = e2e.boards.remove("ashby", "cohere", ML_INTERN_ID)
    assert await e2e.run() == []
    rows = await e2e.postings()
    assert rows[ML_INTERN].status == "closed" and rows[ML_INTERN].closed_at is not None
    assert sum(p.status == "closed" for p in rows.values()) == 1

    e2e.boards.restore("ashby", "cohere", removed)
    assert await e2e.run() == []  # reopened, still never re-notified
    rows = await e2e.postings()
    ml = rows[ML_INTERN]
    assert (ml.status, ml.closed_at) == ("open", None)
    # Same content: extraction and embedding are kept, so no LLM or embedding call is spent on it.
    assert (ml.extraction_status, ml.extracted_at, ml.embedded_hash) == ("ok", extracted_at[ML_INTERN], ml.content_hash)
    assert ml.score == pytest.approx(e2e.expected_score(ML_INTERN), abs=1e-4)
    assert (len(e2e.llm.calls), len(e2e.llm.embed_calls)) == (llm_calls, embed_calls)
    assert len(e2e.channel.sent) == 2 and e2e.channel.calls == 2
    notifications = await e2e.notifications()
    assert len(notifications) == 2 and (notifications[ml.id].status, notifications[ml.id].attempts) == ("sent", 1)

    # ---- chat: the matched postings are findable through the agent's search_postings tool -----------
    e2e.chat.responses = [
        tool_call_response("search_postings", {"query": "", "filters": {"term": "Winter 2027", "location": "Remote"}}),
        Response(text="Cohere has two Winter 2027 co-ops that match your resume."),
    ]
    reply = await e2e.app.handle(Incoming(channel="telegram", chat_id="owner", text="any new AI co-ops?"))
    assert reply == "Cohere has two Winter 2027 co-ops that match your resume."
    tool_msg = e2e.chat.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    result = json.loads(tool_msg["content"])
    assert result["ranking"] == "score"
    top = {r["title"]: r for r in result["results"][:2]}
    assert set(top) == set(TARGETS)
    for title in TARGETS:
        assert top[title]["url"] == rows[title].url and top[title]["notified"] is True
        assert top[title]["company"] == "Cohere" and top[title]["locations"] == ["Remote (Canada)"]
    others = {r["title"]: r for r in result["results"][2:]}
    assert RESEARCH_INTERN in others and others[RESEARCH_INTERN]["notified"] is False
    assert FAIRE_INTERN not in others  # San Francisco only: filtered out by location
