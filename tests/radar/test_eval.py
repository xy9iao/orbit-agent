"""Eval harness (M4.4): label loading, per-field scoring, report text, kv summary and dry run."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from orbit.core.providers.base import Response
from orbit.core.router import ModelRouter
from orbit.radar.eval import (
    KV_LAST_EVAL,
    UNVERIFIED_BANNER,
    EvalAbortedError,
    LabelFileError,
    dry_run,
    format_report,
    is_unverified,
    jaccard,
    load_labels,
    run_eval,
    save_last_eval,
)
from orbit.radar.models import RoleType
from orbit.storage.db import Database
from tests.conftest import FakeProvider, make_config

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "radar"
SMALL = FIXTURES / "labels_small.yaml"
NOW = datetime(2026, 10, 15, 12, 0, 0)

ITEM1_OK = {
    "is_internship": True,
    "terms": ["Summer 2027"],
    "locations": ["Toronto, ON"],
    "remote_ok": False,
    "canada_eligible": True,
    "role_type": "ml_ai",
    "tech_stack": ["kubernetes", "python", "pytorch"],  # order does not matter
}
ITEM2_PARTIAL = {
    "is_internship": True,
    "terms": ["Fall 2027"],  # missing Winter 2028: wrong, Jaccard 1/2
    "locations": ["Remote - Canada"],  # normalizes to Remote (Canada): right
    "remote_ok": False,  # wrong
    "canada_eligible": True,
    "role_type": "swe",
    "tech_stack": ["go", "react"],  # missing postgresql: wrong, Jaccard 2/3
}


class TitleProvider(FakeProvider):
    """Answers by posting title, so concurrent calls get the right reply regardless of order."""

    def __init__(self, replies: dict[str, list[str]]):
        super().__init__("cheap")
        self.replies = {k: list(v) for k, v in replies.items()}

    async def complete(self, model, messages, tools=None, **params):
        self.calls.append({"model": model, "messages": messages, "tools": tools, "params": params})
        user = messages[1]["content"]
        for title, queue in self.replies.items():
            if f"Title: {title}\n" in user:
                return Response(text=queue.pop(0), usage={"prompt_tokens": 10, "completion_tokens": 5})
        raise AssertionError(f"unexpected posting: {user[:80]}")


class RaisingProvider(FakeProvider):
    async def complete(self, model, messages, tools=None, **params):
        self.calls.append({"model": model})
        raise ConnectionError("connection reset by peer")


def make_router(provider: FakeProvider) -> ModelRouter:
    return ModelRouter(make_config(), {"cheap": provider, "openai": FakeProvider("openai")})


def scripted_router() -> tuple[ModelRouter, TitleProvider]:
    provider = TitleProvider(
        {
            "Machine Learning Intern (Summer 2027)": [json.dumps(ITEM1_OK)],
            "Software Engineering Co-op (Fall 2027 / Winter 2028)": [json.dumps(ITEM2_PARTIAL)],
            "Senior Data Engineer": ["not json", '{"is_internship": "maybe"'],
        }
    )
    return make_router(provider), provider


def write_labels(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "labels.yaml"
    path.write_text(text, encoding="utf-8")
    return path


ONE_ITEM = """\
status: verified
items:
  - id: "lever:x:1"
    source: lever
    company: X
    title: Intern
    location_raw: Toronto
    url: https://example.com
    description_text: text
    expected:
      is_internship: true
      terms: []
      locations: []
      remote_ok: false
      canada_eligible: true
      role_type: swe
      tech_stack: []
"""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_load_small_labels_normalizes_expected():
    labels = load_labels(SMALL)

    assert labels.status == "UNVERIFIED — needs human review"
    assert labels.version == 1 and labels.path == str(SMALL)
    assert [i.id for i in labels.items] == ["greenhouse:acme:1001", "lever:beta:abc-2002", "ashby:gamma:c3"]
    first = labels.items[0].expected
    assert first.terms == ["Summer 2027"]
    assert first.locations == ["Toronto, ON"]
    assert first.role_type is RoleType.ml_ai
    assert first.tech_stack == ["python", "pytorch", "kubernetes"]
    second = labels.items[1]
    assert second.department is None and second.label_notes == "8-month co-op spanning two terms."
    assert second.expected.locations == ["Remote (Canada)"]
    assert second.expected.tech_stack == ["go", "postgresql", "react"]
    assert labels.items[2].expected.terms == []


def test_is_unverified_is_case_insensitive(tmp_path: Path):
    assert is_unverified(load_labels(SMALL))
    verified = load_labels(write_labels(tmp_path, ONE_ITEM))
    assert not is_unverified(verified)
    assert is_unverified(verified.model_copy(update={"status": "draft (unverified)"}))


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("      terms: []", '      terms: ["whenever works"]', "expected.terms"),
        ("      role_type: swe\n", "", "missing expected field(s): role_type"),
        ("      tech_stack: []", "      tech_stack: []\n      remote: true", "unknown expected field(s): remote"),
        ("    source: lever", "    source: workday", "source must be one of"),
        ("    title: Intern\n", "", "title"),
        ("    url: https://example.com", "    url: https://example.com\n    extra: 1", "extra"),
    ],
)
def test_malformed_item_names_the_item(tmp_path: Path, old: str, new: str, message: str):
    assert old in ONE_ITEM
    path = write_labels(tmp_path, ONE_ITEM.replace(old, new))
    with pytest.raises(LabelFileError) as info:
        load_labels(path)
    assert "label item lever:x:1 (#1)" in str(info.value)
    assert message in str(info.value)


def test_file_level_errors(tmp_path: Path):
    with pytest.raises(LabelFileError, match="not found"):
        load_labels(tmp_path / "missing.yaml")
    with pytest.raises(LabelFileError, match="status"):
        load_labels(write_labels(tmp_path, ONE_ITEM.replace("status: verified\n", "")))
    with pytest.raises(LabelFileError, match="items"):
        load_labels(write_labels(tmp_path, "status: x\nitems: []\n"))
    with pytest.raises(LabelFileError, match="not valid YAML"):
        load_labels(write_labels(tmp_path, "status: [unclosed\n"))
    doubled = ONE_ITEM + ONE_ITEM.split("items:\n", 1)[1]
    with pytest.raises(LabelFileError, match=r"lever:x:1 \(#2\): duplicate id"):
        load_labels(write_labels(tmp_path, doubled))
    long_text = ONE_ITEM.replace("description_text: text", "description_text: " + "a" * 6001)
    with pytest.raises(LabelFileError, match="description_text"):
        load_labels(write_labels(tmp_path, long_text))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_jaccard():
    assert jaccard([], []) == 1.0
    assert jaccard(["a"], []) == 0.0
    assert jaccard(["a", "b"], ["b", "a"]) == 1.0
    assert jaccard(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)


async def test_run_eval_scores_each_field():
    router, provider = scripted_router()
    report = await run_eval(router, load_labels(SMALL), now=NOW)

    assert report.model == "extract-small" and report.n == 3
    assert len(provider.calls) == 4  # the bad item is retried once
    acc = {f.name: (f.correct, f.n) for f in report.fields}
    assert acc == {
        "is_internship": (2, 3),
        "terms": (1, 3),
        "locations": (2, 3),
        "remote_ok": (1, 3),
        "canada_eligible": (2, 3),
        "role_type": (2, 3),
        "tech_stack": (1, 3),
    }
    assert report.field("terms").mean_jaccard == pytest.approx((1 + 0.5 + 0) / 3)
    assert report.field("locations").mean_jaccard == pytest.approx(2 / 3)
    assert report.field("tech_stack").mean_jaccard == pytest.approx((1 + 2 / 3 + 0) / 3)
    assert report.field("remote_ok").mean_jaccard is None
    assert report.field("is_internship").accuracy == pytest.approx(2 / 3)

    by_id = {r.id: r for r in report.items}
    assert by_id["greenhouse:acme:1001"].wrong == ()
    assert set(by_id["lever:beta:abc-2002"].wrong) == {"terms", "remote_ok", "tech_stack"}
    assert [r.id for r in report.failures] == ["ashby:gamma:c3"]
    assert by_id["ashby:gamma:c3"].error.startswith("after retry:")


async def test_run_eval_raises_when_task_unresolvable():
    openai = FakeProvider("openai")
    router = ModelRouter(make_config(), {"openai": openai})  # no "cheap" provider: no API key
    with pytest.raises(RuntimeError, match="not configured"):
        await run_eval(router, load_labels(SMALL))
    assert openai.calls == []


async def test_run_eval_aborts_on_repeated_provider_errors():
    provider = RaisingProvider("cheap")
    with pytest.raises(EvalAbortedError, match="consecutive provider errors"):
        await run_eval(make_router(provider), load_labels(SMALL), concurrency=1)
    assert len(provider.calls) == 3


# ---------------------------------------------------------------------------
# Output and kv summary
# ---------------------------------------------------------------------------


async def test_format_report_banner_and_table():
    router, _ = scripted_router()
    report = await run_eval(router, load_labels(SMALL), now=NOW)

    text = format_report(report, unverified=True)
    assert text.splitlines()[1] == UNVERIFIED_BANNER
    assert "⚠ UNVERIFIED LABELS" in text and "evals/radar_labels.yaml" in text
    lines = {line.split()[0]: line.split() for line in text.splitlines() if line.strip()}
    assert lines["is_internship"] == ["is_internship", "66.7%", "-", "3"]
    assert lines["terms"] == ["terms", "33.3%", "0.50", "3"]
    assert lines["tech_stack"] == ["tech_stack", "33.3%", "0.56", "3"]
    assert "Failed extractions: 1" in text
    assert "  ashby:gamma:c3: after retry:" in text
    assert "  remote_ok: lever:beta:abc-2002" in text

    plain = format_report(report, unverified=False)
    assert "UNVERIFIED" not in plain
    assert "is_internship" in plain


async def test_save_last_eval_writes_kv(db: Database):
    router, _ = scripted_router()
    report = await run_eval(router, load_labels(SMALL), now=NOW)

    saved = await save_last_eval(db, report, "evals/radar_labels.yaml", True)
    stored = await db.kv_get(KV_LAST_EVAL)
    assert stored == saved
    assert stored["at"] == "2026-10-15T12:00:00"
    assert stored["model"] == "extract-small"
    assert stored["label_file"] == "evals/radar_labels.yaml"
    assert stored["unverified"] is True
    assert stored["n"] == 3 and stored["failures"] == 1
    assert stored["fields"]["terms"] == {"accuracy": pytest.approx(0.3333), "n": 3, "mean_jaccard": 0.5}
    assert stored["fields"]["role_type"] == {"accuracy": pytest.approx(0.6667), "n": 3}
    assert set(stored["fields"]) == {
        "is_internship", "terms", "locations", "remote_ok", "canada_eligible", "role_type", "tech_stack"
    }  # fmt: skip


def test_dry_run_counts_and_banner(tmp_path: Path):
    text = dry_run(load_labels(SMALL))
    assert UNVERIFIED_BANNER in text
    assert "Items: 3 (2 internship, 1 non-internship)" in text
    assert "By source: greenhouse 1, lever 1, ashby 1" in text
    assert "Items with label notes: 1" in text
    assert "no extraction calls" in text

    assert "UNVERIFIED" not in dry_run(load_labels(write_labels(tmp_path, ONE_ITEM)))


def test_label_item_is_a_posting_for_build_messages():
    from orbit.radar.extract import build_messages

    item = load_labels(SMALL).items[1]
    user = build_messages(item, company=item.company)[1]["content"]
    assert "Company: Beta Corp" in user and "Department: not listed" in user
    assert "Location (as listed): Remote - Canada" in user


# ---------------------------------------------------------------------------
# The real label set (M4.5)
# ---------------------------------------------------------------------------

REAL_LABELS = Path(__file__).resolve().parent.parent.parent / "evals" / "radar_labels.yaml"


def test_real_label_file_loads_and_stays_unverified():
    labels = load_labels(REAL_LABELS)

    assert labels.status == "UNVERIFIED — needs human review"
    assert is_unverified(labels)
    assert len(labels.items) == 30
    interns = [i for i in labels.items if i.expected.is_internship]
    assert len(interns) >= 12
    assert len(labels.items) - len(interns) >= 8
    assert {i.source for i in labels.items} == {"greenhouse", "lever", "ashby"}
    assert all(i.description_text and len(i.description_text) <= 6000 for i in labels.items)
    for item in labels.items:  # ids follow <source>:<company_slug>:<external_id>
        assert item.id.startswith(item.source + ":") and item.id.count(":") >= 2

    text = dry_run(labels)
    assert UNVERIFIED_BANNER in text
    assert f"Items: 30 ({len(interns)} internship, {30 - len(interns)} non-internship)" in text


def test_real_labels_are_written_in_canonical_form():
    """A label that normalization would change is a typo risk: the reviewer sees one value, eval scores another."""
    raw = yaml.safe_load(REAL_LABELS.read_text(encoding="utf-8"))
    for raw_item, item in zip(raw["items"], load_labels(REAL_LABELS).items, strict=True):
        assert raw_item["expected"] == item.expected.model_dump(mode="json"), item.id
