"""LLM extraction (M4.2) with FakeProvider: prompt, retry rule, prefilter, and the DB batch."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, update

from orbit.core.providers.base import Response
from orbit.core.router import ModelRouter
from orbit.radar import extract as ex
from orbit.radar.extract import (
    MAX_DESCRIPTION_CHARS,
    PREFILTER_MODEL,
    TRUNCATION_MARKER,
    ExtractSummary,
    build_messages,
    extract_pending,
    extract_posting,
    phd_requirement,
    prefilter,
    skip_reason,
    truncate_description,
)
from orbit.radar.models import PostingExtraction
from orbit.storage.db import Database
from orbit.storage.models import Company, Posting
from tests.conftest import FakeProvider, make_config

T0 = datetime(2026, 9, 1, 9, 0, 0)
NOW = datetime(2026, 9, 2, 12, 0, 0)

GOOD = {
    "is_internship": True,
    "terms": ["summer '27", "Fall 2027"],
    "locations": ["Toronto, Ontario, Canada"],
    "remote_ok": False,
    "canada_eligible": True,
    "role_type": "Machine Learning",
    "tech_stack": ["Python", "PyTorch", "k8s"],
}
GOOD_NORMALIZED = {
    "is_internship": True,
    "terms": ["Summer 2027", "Fall 2027"],
    "locations": ["Toronto, ON"],
    "remote_ok": False,
    "canada_eligible": True,
    "role_type": "ml_ai",
    "tech_stack": ["python", "pytorch", "kubernetes"],
}
BAD_TERMS = {**GOOD, "terms": ["whenever works"]}


def reply(payload: Any) -> Response:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return Response(text=text, usage={"prompt_tokens": 10, "completion_tokens": 5})


def posting(**kw: Any) -> SimpleNamespace:
    fields = {
        "title": "Machine Learning Intern (Summer 2027)",
        "location_raw": "Toronto, ON",
        "department": "Research",
        "description_text": "Train models with PyTorch.\n\nMay to August 2027.",
        "company": "Acme AI",
    }
    fields.update(kw)
    return SimpleNamespace(**fields)


class RaisingProvider(FakeProvider):
    async def complete(self, model, messages, tools=None, **params):
        self.calls.append({"model": model, "messages": messages, "tools": tools, "params": params})
        raise ConnectionError("connection reset by peer\nsecond line\nthird line\nfourth line")


def make_router(provider: FakeProvider) -> ModelRouter:
    return ModelRouter(make_config(), {"cheap": provider, "openai": FakeProvider("openai")})


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def test_build_messages_has_field_definitions_and_posting():
    messages = build_messages(posting())

    assert [m["role"] for m in messages] == ["system", "user"]
    system, user = messages[0]["content"], messages[1]["content"]
    for name, field in PostingExtraction.model_fields.items():
        assert f"- {name}: {field.description}" in system
    assert "Spring" in system and "Waterloo" in system
    assert "Company: Acme AI" in user
    assert "Title: Machine Learning Intern (Summer 2027)" in user
    assert "Location (as listed): Toronto, ON" in user
    assert "Department: Research" in user
    assert user.endswith("Train models with PyTorch.\n\nMay to August 2027.")


def test_build_messages_explicit_company_and_missing_fields():
    user = build_messages(posting(department=None, location_raw="", description_text=""), company="Beta")[1]["content"]
    assert "Company: Beta" in user
    assert "Department: not listed" in user and "Location (as listed): not listed" in user
    assert "(no description)" in user


def test_truncate_description_cuts_at_paragraph_boundary():
    para = "word " * 199 + "end."  # 999 chars, 1001 with its separator
    text = "\n\n".join([para] * 20)  # ~20k chars
    out = truncate_description(text)

    assert len(out) <= MAX_DESCRIPTION_CHARS + len(TRUNCATION_MARKER)
    assert out.endswith("end." + TRUNCATION_MARKER)
    assert out.count("end.") == 11  # the 12th paragraph would end past 12,000 chars
    assert truncate_description("short\n\ntext") == "short\n\ntext"


def test_truncate_description_without_breaks_hard_cuts():
    out = truncate_description("x" * 20_000, limit=100)
    assert out == "x" * 100 + TRUNCATION_MARKER


def test_build_messages_truncates_long_descriptions():
    user = build_messages(posting(description_text="a b " * 10_000))[1]["content"]
    assert TRUNCATION_MARKER in user
    assert len(user) < MAX_DESCRIPTION_CHARS + 500


# ---------------------------------------------------------------------------
# Prefilter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Sr. Backend Developer",
        "Staff Machine Learning Engineer",
        "Principal Engineer, Payments",
        "Engineering Manager, Platform",
        "Director of Engineering",
        "Head of Data",
        "VP, Product",
        "Solutions Architect",
        "Software Engineer III",
        "Tech Lead - Mobile",
    ],
)
def test_prefilter_skips_obvious_non_student_titles(title):
    assert prefilter(posting(title=title, description_text="Join our team.")) is True


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineering Intern",
        "Software Developer Co-op (Winter 2027)",
        "Machine Learning Coop",
        "Product Manager Intern",
        "Senior Thesis Student Researcher",
        "Stagiaire en développement logiciel",
        "New Grad Software Engineer",
        "Software Engineer",
        "Software Engineer II, New Grad",
        "Leadership Development Program",
        "Data Analyst",
        "Architecture Intern",
    ],
)
def test_prefilter_keeps_possible_student_roles(title):
    assert prefilter(posting(title=title, description_text="Join our team.")) is False


@pytest.mark.parametrize(
    "title",
    [
        "2026 Intern, PhD Research Scientist",
        "PhD Data Scientist, Intern",
        "Research Intern (Ph.D.)",
        "Ph.D Machine Learning Co-op",
        "Ph D Student Researcher",
        "Internship for PhDs",
        "phd software engineering intern",
    ],
)
def test_prefilter_skips_phd_titles_despite_student_words(title):
    outcome = skip_reason(posting(title=title, description_text="This internship is for students."))
    assert outcome is not None and "PhD role" in outcome


@pytest.mark.parametrize("title", ["Software Engineering Intern", "Graphd Engineer Intern", "Alphadev Co-op"])
def test_prefilter_phd_rule_needs_the_whole_word(title):
    assert prefilter(posting(title=title, description_text="This internship is for students.")) is False


def test_prefilter_phd_in_description_only_is_kept():
    assert prefilter(posting(title="Research Intern", description_text="Open to Bachelor's and PhD students.")) is False


@pytest.mark.parametrize(
    "description",
    [
        "Please Note: To be eligible, you must be currently pursuing a PhD in Machine Learning or NLP.",
        "Requirements:\n- Pursuing PhD degree in Computer Science or a related field",
        "This internship is for PhD students only.",
        "Currently enrolled in a PhD program in statistics or psychology",
        "- Working towards a Ph.D. in Robotics",
        "The role is only open to PhD candidates.",
        "You must be a current PhD student.",
        "A PhD is required.",
        "Preferred qualifications:\n- Publications\nMinimum qualifications:\n- Currently pursuing a PhD in ML",
    ],
)
def test_prefilter_skips_descriptions_that_require_a_phd(description):
    reason = skip_reason(posting(title="Research Intern", description_text=description))
    assert reason is not None and reason.startswith("description requires a PhD")


@pytest.mark.parametrize(
    "description",
    [
        "PhD preferred.",
        "- Currently pursuing a PhD is a plus",
        "Ideally pursuing a PhD in ML.",
        "Preferred Qualifications:\n- Currently pursuing a PhD in ML\n- Publications at NeurIPS",
        "Nice to have:\n- Enrolled in a PhD program",
        "Working towards a Master's or PhD degree in Computer Science",
        "Currently enrolled in or recently graduated from a Master's or PhD program",
        "Through pursuit of a Bachelor's, Master's, or PhD degree in computer science",
        "Currently pursuing a PhD or Master's degree in CS",
        "Pursuing a PhD/MS in Robotics",
        "Bachelors or MS/PhD degree in Computer Science",
        "This internship is for undergraduate students.",
    ],
)
def test_prefilter_keeps_descriptions_where_a_phd_is_optional(description):
    assert skip_reason(posting(title="Research Intern", description_text=description)) is None


def test_phd_requirement_returns_the_matching_phrase():
    assert phd_requirement("About us\n\nYou must be currently pursuing a PhD in NLP.") == "pursuing a PhD"
    assert phd_requirement("") is None


def test_prefilter_description_opening_vetoes_skip():
    assert prefilter(posting(title="Lead Engineer", description_text="This internship is for students.")) is False
    late = "Join our team. " * 100 + "We also hire interns."
    assert prefilter(posting(title="Lead Engineer", description_text=late)) is True


# ---------------------------------------------------------------------------
# extract_posting
# ---------------------------------------------------------------------------


async def test_success_normalizes_and_passes_strict_schema():
    fake = FakeProvider("cheap", [reply(GOOD)])

    outcome = await extract_posting(make_router(fake), posting())

    assert outcome.status == "ok" and outcome.error is None
    assert outcome.extraction == GOOD_NORMALIZED
    assert outcome.model == "extract-small"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["model"] == "extract-small" and call["tools"] is None
    rf = call["params"]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"] == PostingExtraction.openai_json_schema()
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"]["additionalProperties"] is False


async def test_code_fenced_reply_is_accepted():
    fake = FakeProvider("cheap", [reply("```json\n" + json.dumps(GOOD) + "\n```")])
    outcome = await extract_posting(make_router(fake), posting())
    assert outcome.status == "ok" and outcome.extraction == GOOD_NORMALIZED


async def test_invalid_json_then_valid_retry_is_ok():
    fake = FakeProvider("cheap", [reply("{not json"), reply(GOOD)])

    outcome = await extract_posting(make_router(fake), posting())

    assert outcome.status == "ok" and outcome.extraction == GOOD_NORMALIZED
    assert len(fake.calls) == 2
    retry_messages = fake.calls[1]["messages"]
    assert len(retry_messages) == 4
    assert retry_messages[2] == {"role": "assistant", "content": "{not json"}
    assert retry_messages[3]["role"] == "user"
    assert "JSONDecodeError" in retry_messages[3]["content"]
    assert fake.calls[1]["params"]["response_format"] == fake.calls[0]["params"]["response_format"]


async def test_schema_violation_then_valid_retry_is_ok():
    fake = FakeProvider("cheap", [reply(BAD_TERMS), reply(GOOD)])
    outcome = await extract_posting(make_router(fake), posting())
    assert outcome.status == "ok"
    assert "terms" in fake.calls[1]["messages"][3]["content"]


async def test_schema_violation_twice_fails_with_error():
    fake = FakeProvider("cheap", [reply(BAD_TERMS), reply(BAD_TERMS), reply(GOOD)])

    outcome = await extract_posting(make_router(fake), posting())

    assert outcome.status == "failed" and outcome.extraction is None
    assert outcome.error.startswith("after retry: ValidationError: terms")
    assert "whenever works" in outcome.error
    assert len(outcome.error) <= 1000
    assert outcome.model == "extract-small"
    assert len(fake.calls) == 2  # exactly one retry


async def test_missing_field_is_a_validation_error():
    partial = {k: v for k, v in GOOD.items() if k != "canada_eligible"}
    fake = FakeProvider("cheap", [reply(partial), reply("[]")])
    outcome = await extract_posting(make_router(fake), posting())
    assert outcome.status == "failed" and "ValidationError" in outcome.error


async def test_provider_exception_fails_without_retry():
    fake = RaisingProvider("cheap")

    outcome = await extract_posting(make_router(fake), posting())

    assert outcome.status == "failed" and outcome.provider_error is True
    assert outcome.error == "ConnectionError: connection reset by peer\nsecond line\nthird line"
    assert len(fake.calls) == 1


def test_describe_error_is_capped():
    assert len(ex.describe_error(ValueError("x" * 5000))) == 1000


# ---------------------------------------------------------------------------
# extract_pending (real Postgres)
# ---------------------------------------------------------------------------


async def _company(db: Database) -> Company:
    async with db.session() as s:
        company = Company(slug="acme", name="Acme AI", source="greenhouse", board_token="acme")
        s.add(company)
        await s.commit()
        return company


async def _posting(
    db: Database,
    company: Company,
    external_id: str,
    *,
    minutes: int = 0,
    title: str = "Software Engineering Intern",
    status: str = "open",
    extraction_status: str = "pending",
) -> int:
    async with db.session() as s:
        row = Posting(
            source="greenhouse",
            external_id=external_id,
            company_id=company.id,
            title=title,
            url=f"https://example.com/{external_id}",
            location_raw="Toronto, ON",
            department="Engineering",
            description_text=f"Posting {external_id}: build things.",
            raw={},
            content_hash=f"hash-{external_id}",
            status=status,
            first_seen_at=T0 + timedelta(minutes=minutes),
            last_seen_at=T0 + timedelta(minutes=minutes),
            extraction_status=extraction_status,
            extraction_error="old error" if extraction_status == "failed" else None,
        )
        s.add(row)
        await s.commit()
        return row.id


async def _rows(db: Database) -> dict[str, Posting]:
    async with db.session() as s:
        return {p.external_id: p for p in (await s.execute(select(Posting))).scalars()}


async def test_extract_pending_persists_ok_failed_and_skipped(db: Database):
    company = await _company(db)
    await _posting(db, company, "ok", minutes=3)
    await _posting(db, company, "bad", minutes=2)
    await _posting(db, company, "senior", minutes=1, title="Senior Software Engineer")
    # Newest first, so "ok" gets the first reply and "bad" the next two.
    fake = FakeProvider("cheap", [reply(GOOD), reply("nope"), reply(BAD_TERMS)])

    summary = await extract_pending(db, make_router(fake), limit=10, concurrency=1, now=NOW)

    assert summary == ExtractSummary(ok=1, failed=1, skipped=1)
    rows = await _rows(db)
    ok = rows["ok"]
    assert ok.extraction_status == "ok" and ok.extraction == GOOD_NORMALIZED
    assert ok.extraction_error is None and ok.extraction_model == "extract-small" and ok.extracted_at == NOW
    bad = rows["bad"]
    assert bad.extraction_status == "failed" and bad.extraction is None
    assert bad.extraction_error.startswith("after retry: ValidationError")
    assert bad.extraction_model == "extract-small" and bad.extracted_at == NOW
    senior = rows["senior"]
    assert senior.extraction_status == "skipped" and senior.extraction_model == PREFILTER_MODEL
    assert senior.extraction["is_internship"] is False and "Senior" in senior.extraction["skipped_reason"]
    assert len(fake.calls) == 3  # the skipped posting made no LLM call
    assert "Company: Acme AI" in fake.calls[0]["messages"][1]["content"]


async def test_prefilter_skip_makes_no_llm_call(db: Database):
    company = await _company(db)
    await _posting(db, company, "1", title="Staff Engineer")
    fake = FakeProvider("cheap")

    summary = await extract_pending(db, make_router(fake), limit=5)

    assert summary == ExtractSummary(skipped=1)
    assert fake.calls == []
    async with db.session() as s:
        hits = (await s.execute(select(Posting.id).where(Posting.extraction["is_internship"].astext == "false"))).all()
    assert len(hits) == 1  # the M5.4 filter shape works on skipped rows


async def test_extract_pending_respects_limit_newest_first(db: Database):
    company = await _company(db)
    for i in range(4):
        await _posting(db, company, str(i), minutes=i)
    fake = FakeProvider("cheap", [reply(GOOD)] * 4)

    summary = await extract_pending(db, make_router(fake), limit=2)

    assert summary.ok == 2
    statuses = {k: p.extraction_status for k, p in (await _rows(db)).items()}
    assert statuses == {"0": "pending", "1": "pending", "2": "ok", "3": "ok"}
    assert await extract_pending(db, make_router(fake), limit=0) == ExtractSummary()


async def test_extract_pending_skips_closed_and_done_and_retries_failed_on_request(db: Database):
    company = await _company(db)
    await _posting(db, company, "closed", status="closed")
    await _posting(db, company, "done", extraction_status="ok")
    await _posting(db, company, "failed", extraction_status="failed")
    fake = FakeProvider("cheap", [reply(GOOD)])

    assert await extract_pending(db, make_router(fake), limit=10) == ExtractSummary()
    assert fake.calls == []

    summary = await extract_pending(db, make_router(fake), limit=10, retry_failed=True)

    assert summary == ExtractSummary(ok=1)
    rows = await _rows(db)
    assert rows["failed"].extraction_status == "ok" and rows["failed"].extraction_error is None
    assert rows["closed"].extraction_status == "pending" and rows["done"].extraction is None


async def test_content_hash_guard_discards_stale_result(db: Database):
    company = await _company(db)
    pid = await _posting(db, company, "1")

    class ChangingProvider(FakeProvider):
        async def complete(self, model, messages, tools=None, **params):
            # A poll lands while the LLM is thinking: new content, reset to pending.
            async with db.session() as s:
                await s.execute(update(Posting).where(Posting.id == pid).values(content_hash="hash-new"))
                await s.commit()
            return await super().complete(model, messages, tools, **params)

    fake = ChangingProvider("cheap", [reply(GOOD)])

    summary = await extract_pending(db, make_router(fake), limit=5)

    assert summary == ExtractSummary(stale=1)
    row = (await _rows(db))["1"]
    assert row.content_hash == "hash-new"
    assert row.extraction_status == "pending" and row.extraction is None and row.extracted_at is None


async def test_provider_errors_mark_failed_and_stop_the_batch(db: Database):
    company = await _company(db)
    for i in range(5):
        await _posting(db, company, str(i), minutes=i)
    fake = RaisingProvider("cheap")

    summary = await extract_pending(db, make_router(fake), limit=10, concurrency=1)

    assert summary == ExtractSummary(failed=ex.MAX_CONSECUTIVE_PROVIDER_ERRORS, aborted=True)
    rows = await _rows(db)
    assert [rows[str(i)].extraction_status for i in range(5)] == ["pending", "pending", "failed", "failed", "failed"]
    assert rows["4"].extraction_error.startswith("ConnectionError: connection reset by peer")


async def test_unresolvable_task_raises_before_touching_rows(db: Database):
    company = await _company(db)
    await _posting(db, company, "1")
    router = ModelRouter(make_config(), {"openai": FakeProvider("openai")})  # no "cheap" provider

    with pytest.raises(RuntimeError, match="not configured"):
        await extract_pending(db, router, limit=5)
    assert (await _rows(db))["1"].extraction_status == "pending"
