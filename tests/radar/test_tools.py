"""Radar chat tools (M6.2): search_postings and set_radar_goal, through the registry and the agent loop.

Semantic ranking uses FakeProvider, whose embed is conftest.fake_embedding: the query "AI co-op" embeds to
Q = fake_embedding("AI co-op"). Seeded posting vectors are built as `sim * Q + sqrt(1 - sim^2) * R` with R a
unit vector orthogonal to Q, so each posting's cosine similarity to the query is exactly `sim`.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from orbit.config import Config
from orbit.core.agent import Agent
from orbit.core.providers.base import Response
from orbit.core.router import ModelRouter
from orbit.core.tools import registry
from orbit.core.tools.registry import ToolContext
from orbit.storage.db import Database
from orbit.storage.models import Company, Notification, Posting, Profile
from tests.conftest import FakeProvider, fake_embedding, make_config, tool_call_response
from tests.radar.test_extract import GOOD_NORMALIZED

QUERY = "AI co-op"
NOW = datetime.now().replace(microsecond=0)


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def at_similarity(sim: float, seed: str) -> list[float]:
    """A unit vector whose cosine similarity to fake_embedding(QUERY) is exactly `sim`."""
    q = fake_embedding(QUERY)
    r = fake_embedding(seed)
    dot = sum(a * b for a, b in zip(q, r, strict=True))
    r = _unit([b - dot * a for a, b in zip(q, r, strict=True)])
    rest = math.sqrt(1 - sim * sim)
    return [sim * a + rest * b for a, b in zip(q, r, strict=True)]


def _extraction(**kw: Any) -> dict[str, Any]:
    return {**GOOD_NORMALIZED, **kw}


async def _seed(db: Database) -> None:
    """Seven postings; see each comment. Open extracted internships are tor-ml, remote-data, van-swe, tor-swe."""
    async with db.session() as s:
        acme = Company(slug="acme", name="Acme AI", source="greenhouse", board_token="acme")
        maple = Company(slug="maple", name="Maple Data", source="lever", board_token="maple")
        s.add_all([acme, maple])
        await s.flush()

        def row(company: Company, key: str, title: str, *, days: int, **kw: Any) -> Posting:
            fields: dict[str, Any] = {
                "source": company.source,
                "external_id": key,
                "company_id": company.id,
                "title": title,
                "url": f"https://jobs.example.com/{key}",
                "location_raw": "Toronto, ON",
                "description_text": f"{title}. Build things with a friendly team.",
                "raw": {},
                "content_hash": f"hash-{key}",
                "first_seen_at": NOW - timedelta(days=days),
                "last_seen_at": NOW,
                "extraction_status": "ok",
                "extraction": _extraction(),
            }
            fields.update(kw)
            return Posting(**fields)

        rows = [
            # Toronto ML co-op: Summer/Fall 2027, ml_ai, the query's exact vector, already notified.
            row(acme, "tor-ml", "Machine Learning Co-op", days=1, score=0.8, embedding=at_similarity(1.0, "a")),
            # Remote (Canada) data role, no term stated.
            row(
                maple, "remote-data", "Data Science Intern", days=3, score=0.7,
                location_raw="Remote - Canada", embedding=at_similarity(0.3, "b"),
                extraction=_extraction(
                    terms=[], locations=["Remote (Canada)"], remote_ok=True, role_type="data", tech_stack=[]
                ),
            ),
            # Vancouver SWE, Winter 2027, seen 20 days ago.
            row(
                maple, "van-swe", "Software Engineering Intern", days=20, score=0.6,
                location_raw="Vancouver, BC", embedding=at_similarity(0.6, "c"),
                description_text="Software Engineering Intern. Detail-oriented maintainers welcome.",
                extraction=_extraction(terms=["Winter 2027"], locations=["Vancouver, BC"], role_type="swe"),
            ),
            # Toronto SWE without an embedding; its description mentions AI (keyword fill in semantic mode).
            row(
                acme, "tor-swe", "Backend Intern", days=10, score=0.5,
                description_text="Backend Intern on our AI platform team.",
                extraction=_extraction(terms=["Summer 2027"], role_type="swe"),
            ),
            # Closed Toronto ML internship with the best score.
            row(
                acme, "closed-ml", "ML Research Intern", days=40, score=0.95, status="closed",
                embedding=at_similarity(0.9, "d"),
            ),
            # Not an internship.
            row(acme, "fulltime", "Senior ML Engineer", days=2, extraction=_extraction(is_internship=False)),
            # Not extracted yet: reachable by keyword only, with internship_only=false.
            row(
                maple, "pending", "Backend Developer Co-op (Rust)", days=0, location_raw="Waterloo, ON",
                description_text="Write Rust services.", extraction_status="pending", extraction=None,
            ),
        ]  # fmt: skip
        s.add_all(rows)
        await s.flush()
        s.add(Notification(posting_id=rows[0].id, score=0.8, status="sent"))
        s.add(Notification(posting_id=rows[1].id, score=0.7, status="failed"))
        await s.commit()


def _router(config: Config | None = None, provider: FakeProvider | None = None) -> tuple[ModelRouter, FakeProvider]:
    provider = provider or FakeProvider("cheap")
    return ModelRouter(config or make_config(), {"openai": provider, "cheap": provider}), provider


def _ctx(db: Database, memory, router: ModelRouter | None) -> ToolContext:
    return ToolContext(channel="cli", chat_id="local", db=db, memory=memory, router=router)


async def _search(ctx: ToolContext, query: str = "", **filters: Any) -> dict[str, Any]:
    out = await registry.call("search_postings", {"query": query, "filters": filters}, ctx)
    assert not out.startswith("error"), out
    return json.loads(out)


def _ids(result: dict[str, Any]) -> list[str]:
    return [r["url"].rsplit("/", 1)[1] for r in result["results"]]


# ---- schema -----------------------------------------------------------------------------------------


def test_registry_schemas():
    schemas = {s["function"]["name"]: s["function"] for s in registry.schemas()}
    search = schemas["search_postings"]
    props = search["parameters"]["properties"]
    assert set(props) == {"query", "filters"} and "ctx" not in props
    assert props["query"]["type"] == "string"
    assert props["filters"]["type"] == "object"
    assert search["parameters"]["required"] == []
    assert "Internship Radar" in search["description"]
    # The wrapped Args text is joined, so every filter key is documented for the model.
    for key in ("term:", "location:", "role_type:", "since_days:", "include_closed:", "internship_only:", "limit:"):
        assert key in props["filters"]["description"]
    assert "ml_ai" in props["filters"]["description"] and "max 25" in props["filters"]["description"]

    goal = schemas["set_radar_goal"]
    assert goal["parameters"]["required"] == ["text"]
    assert set(goal["parameters"]["properties"]) == {"text"}
    assert goal["parameters"]["properties"]["text"]["description"]
    assert "goal" in goal["description"]


# ---- search: filters and score ranking ----------------------------------------------------------------


async def test_empty_query_ranks_by_score_with_compact_rows(db, memory):
    await _seed(db)
    result = await _search(_ctx(db, memory, None))
    assert result["ranking"] == "score" and result["count"] == 4
    assert _ids(result) == ["tor-ml", "remote-data", "van-swe", "tor-swe"]
    first = result["results"][0]
    assert first == {
        "company": "Acme AI",
        "title": "Machine Learning Co-op",
        "terms": ["Summer 2027", "Fall 2027"],
        "locations": ["Toronto, ON"],
        "remote_ok": False,
        "role_type": "ml_ai",
        "score": 0.8,
        "first_seen": (NOW - timedelta(days=1)).date().isoformat(),
        "status": "open",
        "url": "https://jobs.example.com/tor-ml",
        "notified": True,
    }
    # Only a sent notification counts; remote-data's failed one does not.
    assert [r["notified"] for r in result["results"]] == [True, False, False, False]
    assert result["filters"] == {"include_closed": False, "internship_only": True, "limit": 10}


@pytest.mark.parametrize(
    ("filters", "expected", "applied"),
    [
        ({"term": "summer 27"}, ["tor-ml", "remote-data", "tor-swe"], {"term": ["Summer 2027"]}),
        ({"term": ["Winter 2027"]}, ["remote-data", "van-swe"], {"term": ["Winter 2027"]}),
        ({"location": "toronto"}, ["tor-ml", "tor-swe"], {"location": "Toronto, ON"}),
        ({"location": "Ontario"}, ["tor-ml", "tor-swe"], {"location": "Ontario, Canada"}),
        ({"location": "Remote"}, ["remote-data"], {"location": "Remote"}),
        ({"location": "remote canada"}, ["remote-data"], {"location": "Remote (Canada)"}),
        ({"location": "Canada"}, ["tor-ml", "remote-data", "van-swe", "tor-swe"], {"location": "Canada"}),
        ({"location": "Vancouver, British Columbia"}, ["van-swe"], {"location": "Vancouver, BC"}),
        ({"location": "Berlin"}, [], {"location": "Berlin"}),
        ({"role_type": "AI"}, ["tor-ml"], {"role_type": "ml_ai"}),
        ({"role_type": "software engineer"}, ["van-swe", "tor-swe"], {"role_type": "swe"}),
        ({"since_days": 5}, ["tor-ml", "remote-data"], {"since_days": 5}),
        ({"since_days": "15"}, ["tor-ml", "remote-data", "tor-swe"], {"since_days": 15}),
        ({"include_closed": True, "role_type": "ml_ai"}, ["closed-ml", "tor-ml"], {"include_closed": True}),
        ({"location": "Toronto", "role_type": "AI", "term": "Fall 2027"}, ["tor-ml"], {}),
        ({"limit": 2}, ["tor-ml", "remote-data"], {"limit": 2}),
    ],
)
async def test_filters(db, memory, filters, expected, applied):
    await _seed(db)
    result = await _search(_ctx(db, memory, None), **filters)
    assert _ids(result) == expected
    assert applied.items() <= result["filters"].items()


async def test_internship_only_false_includes_unextracted_and_non_internships(db, memory):
    await _seed(db)
    ctx = _ctx(db, memory, None)
    result = await _search(ctx, internship_only=False)
    assert set(_ids(result)) == {"tor-ml", "remote-data", "van-swe", "tor-swe", "fulltime", "pending"}
    rows = {r["url"].rsplit("/", 1)[1]: r for r in result["results"]}
    assert rows["fulltime"]["internship"] is False and rows["tor-ml"]["internship"] is True
    assert rows["pending"]["extraction"] == "pending" and rows["pending"]["location_raw"] == "Waterloo, ON"
    assert "terms" not in rows["pending"]
    # Extracted-field filters never match an unextracted posting.
    assert "pending" not in _ids(await _search(ctx, internship_only=False, location="Waterloo"))


async def test_limit_is_capped_at_25(db, memory):
    await _seed(db)
    result = await _search(_ctx(db, memory, None), limit=500)
    assert result["filters"]["limit"] == 25
    assert (await _search(_ctx(db, memory, None), limit=0))["filters"]["limit"] == 1


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ({"city": "Toronto"}, "unknown filter key"),
        ({"term": "whenever"}, "cannot read term"),
        ({"role_type": "astronaut"}, "unknown role_type"),
        ({"since_days": "soon"}, "since_days must be a whole number"),
        ({"since_days": 0}, "since_days must be 1 or more"),
        ({"include_closed": "maybe"}, "include_closed must be true or false"),
    ],
)
async def test_bad_filters_are_error_strings(db, memory, filters, message):
    out = await registry.call("search_postings", {"filters": filters}, _ctx(db, memory, None))
    assert out.startswith("error: ") and message in out


async def test_empty_results_explain_what_to_do(db, memory):
    ctx = _ctx(db, memory, None)
    result = await _search(ctx)
    assert result["count"] == 0 and result["results"] == []
    assert "orbit radar poll" in result["message"]

    await _seed(db)
    result = await _search(ctx, location="Berlin")
    assert "relaxing" in result["message"] and "internship_only=false" in result["message"]


# ---- search: keyword and semantic ranking ----------------------------------------------------------


async def test_keyword_ranking_without_embed_provider(db, memory):
    await _seed(db)
    config = make_config()
    del config.tasks["embed"]  # no embed task: resolve() would fall back to chat, which must not be used
    router, provider = _router(config)
    ctx = _ctx(db, memory, router)

    result = await _search(ctx, "machine learning")
    assert result["ranking"] == "keyword"
    assert _ids(result) == ["tor-ml"]
    assert provider.embed_calls == []

    # Whole words only: "ai" matches "Acme AI" and "our AI platform", not van-swe's "Detail"/"maintainers".
    # tor-ml: title "Co-op" 3 + company 2 + description 1 = 6; tor-swe: company 2 + description 1 = 3.
    result = await _search(ctx, QUERY)
    assert _ids(result) == ["tor-ml", "tor-swe"]
    assert "similarity" not in result["results"][0]

    # Unextracted postings are found by keyword once internship_only is off.
    result = await _search(_ctx(db, memory, None), "rust", internship_only=False)
    assert result["ranking"] == "keyword" and _ids(result) == ["pending"]

    # A query of only stopwords lists by score.
    result = await _search(_ctx(db, memory, None), "any new roles?")
    assert result["ranking"] == "score" and "note" in result


async def test_semantic_ranking_with_keyword_fill(db, memory):
    await _seed(db)
    router, provider = _router()
    result = await _search(_ctx(db, memory, router), QUERY)
    assert result["ranking"] == "semantic"
    assert provider.embed_calls[-1]["texts"] == [QUERY] and provider.embed_calls[-1]["model"] == "embed-small"
    # Embedded postings by similarity, then tor-swe (no embedding) as a keyword match.
    assert _ids(result) == ["tor-ml", "van-swe", "remote-data", "tor-swe"]
    sims = [r.get("similarity") for r in result["results"]]
    assert sims[:3] == pytest.approx([1.0, 0.6, 0.3], abs=1e-3) and sims[3] is None

    filtered = await _search(_ctx(db, memory, router), QUERY, location="Toronto", limit=1)
    assert _ids(filtered) == ["tor-ml"]


async def test_embed_failure_falls_back_to_keyword(db, memory):
    await _seed(db)

    class BrokenEmbed(FakeProvider):
        async def embed(self, model, texts, **params):
            raise ConnectionError("embeddings down")

    router, _ = _router(provider=BrokenEmbed("cheap"))
    result = await _search(_ctx(db, memory, router), QUERY)
    assert result["ranking"] == "keyword" and "semantic search unavailable" in result["note"]
    assert _ids(result) == ["tor-ml", "tor-swe"]


# ---- set_radar_goal ---------------------------------------------------------------------------------


async def _goal(db: Database) -> Profile | None:
    async with db.session() as s:
        return (await s.execute(select(Profile).where(Profile.kind == "goal"))).scalar_one_or_none()


async def test_set_radar_goal_embeds_and_stores(db, memory):
    router, provider = _router()
    ctx = _ctx(db, memory, router)
    out = await registry.call("set_radar_goal", {"text": "  ML infra co-op in Toronto  "}, ctx)
    assert out.startswith("radar goal saved") and "20%" in out and "next radar poll" in out
    assert "no resume" in out
    goal = await _goal(db)
    assert goal is not None and goal.text == "ML infra co-op in Toronto"
    assert list(goal.embedding) == pytest.approx(fake_embedding("ML infra co-op in Toronto"))
    assert provider.embed_calls[-1]["texts"] == ["ML infra co-op in Toronto"]

    async with db.session() as s:
        s.add(Profile(kind="resume", text="r", text_hash="h", embedding=fake_embedding("r")))
        await s.commit()
    again = await registry.call("set_radar_goal", {"text": "ML infra co-op in Toronto"}, ctx)
    assert again.startswith("radar goal unchanged") and "no resume" not in again
    assert len(provider.embed_calls) == 1


async def test_set_radar_goal_errors(db, memory):
    out = await registry.call("set_radar_goal", {"text": "ML co-op"}, _ctx(db, memory, None))
    assert out.startswith("error: no embedding provider")
    config = make_config()
    router = ModelRouter(config, {"openai": FakeProvider("openai")})  # embed's provider 'cheap' has no key
    out = await registry.call("set_radar_goal", {"text": "ML co-op"}, _ctx(db, memory, router))
    assert out.startswith("error: no embedding provider")
    out = await registry.call("set_radar_goal", {"text": "   "}, _ctx(db, memory, _router()[0]))
    assert out.startswith("error: the goal text is empty")
    assert await _goal(db) is None


# ---- agent loop -------------------------------------------------------------------------------------


async def test_agent_calls_search_postings_and_gets_the_toronto_ml_coop(config, db, memory, tmp_path: Path):
    await _seed(db)
    provider = FakeProvider(
        "openai",
        [
            tool_call_response("search_postings", {"query": QUERY, "filters": {"location": "Toronto"}}),
            Response(text="Acme AI has a Machine Learning Co-op in Toronto."),
        ],
    )
    router = ModelRouter(config, {"openai": provider, "cheap": provider})
    persona = tmp_path / "persona"
    persona.mkdir()
    (persona / "system.md").write_text("You are Orbit test.", encoding="utf-8")
    agent = Agent(config.agent, router, registry, memory, db, persona)

    reply = await agent.handle("telegram", "42", "any new AI co-ops in Toronto?")
    assert reply == "Acme AI has a Machine Learning Co-op in Toronto."
    assert "search_postings" in {t["function"]["name"] for t in provider.calls[0]["tools"]}

    tool_msg = provider.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_1"
    result = json.loads(tool_msg["content"])
    assert result["ranking"] == "semantic"  # Agent passed its router into the ToolContext
    top = result["results"][0]
    assert top["title"] == "Machine Learning Co-op" and top["locations"] == ["Toronto, ON"]
    assert top["url"] == "https://jobs.example.com/tor-ml"
    assert {r["title"] for r in result["results"]} == {"Machine Learning Co-op", "Backend Intern"}
