"""PostingExtraction and its normalizers (M4.1): pure functions, no DB or network."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from orbit.radar.models import (
    MAX_TECH_STACK,
    PostingExtraction,
    RoleType,
    is_canadian_location,
    normalize_location,
    normalize_locations,
    normalize_role_type,
    normalize_tech_stack,
    normalize_term,
    normalize_terms,
    parse_terms,
    term_sort_key,
)


def valid_payload(**overrides) -> dict:
    payload = {
        "is_internship": True,
        "terms": ["Summer 2027"],
        "locations": ["Toronto, ON"],
        "remote_ok": False,
        "canada_eligible": True,
        "role_type": "swe",
        "tech_stack": ["python"],
    }
    payload.update(overrides)
    return payload


# --- terms -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Summer 2027", "Summer 2027"),
        ("summer 2027", "Summer 2027"),
        ("  SUMMER   2027 ", "Summer 2027"),
        ("Summer '27", "Summer 2027"),
        ("Summer ’27", "Summer 2027"),
        ("Summer 27", "Summer 2027"),
        ("Summer2027", "Summer 2027"),
        ("2027 Summer", "Summer 2027"),
        ("S27", "Summer 2027"),
        ("s'27", "Summer 2027"),
        ("F26", "Fall 2026"),
        ("W2027", "Winter 2027"),
        ("Spring 2027", "Summer 2027"),  # Waterloo's May-Aug spring term
        ("Spring/Summer 2027", "Summer 2027"),
        ("Fall/Autumn 2026", "Fall 2026"),
        ("Autumn 2026", "Fall 2026"),
        ("Winter 2027 (4 months)", "Winter 2027"),
        ("Winter 2027 (4-month co-op term)", "Winter 2027"),
        ("Summer 2027 Internship", "Summer 2027"),
        ("May 2027", "Summer 2027"),
        ("Starting May 15, 2027", "Summer 2027"),
        ("May–Aug 2027", "Summer 2027"),
        ("May - August 2027", "Summer 2027"),
        ("January 2027", "Winter 2027"),
        ("Jan-Apr 2027", "Winter 2027"),
        ("Sept. 2026", "Fall 2026"),
        ("Dec 2026", "Fall 2026"),
        ("Summer 2027 (May-Aug)", "Summer 2027"),
        ("early summer 2027", "Summer 2027"),
    ],
)
def test_normalize_term(raw, expected):
    assert normalize_term(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("May–Dec 2027", ["Summer 2027", "Fall 2027"]),  # 8-month span
        ("May 2027 - December 2027", ["Summer 2027", "Fall 2027"]),
        ("Summer - Fall 2027", ["Summer 2027", "Fall 2027"]),
        ("Summer 2027 to Fall 2027", ["Summer 2027", "Fall 2027"]),
        ("Sep 2026 - Apr 2027", ["Fall 2026", "Winter 2027"]),
        ("Sep - Apr 2027", ["Fall 2026", "Winter 2027"]),  # start year inferred across New Year
        ("Fall 2026 - Winter 2027", ["Fall 2026", "Winter 2027"]),
        ("May 2027 - Aug 2028", ["Summer 2027", "Fall 2027", "Winter 2028", "Summer 2028"]),  # 16-month PEY
        ("Summer/Fall 2027", ["Summer 2027", "Fall 2027"]),  # a list, not a range
        ("Winter 2027 or Summer 2027", ["Winter 2027", "Summer 2027"]),
        ("Fall 2027, Winter 2027", ["Winter 2027", "Fall 2027"]),  # sorted chronologically
        ("Summer 2027 or Fall", ["Summer 2027", "Fall 2027"]),
    ],
)
def test_parse_terms_expands_spans_and_lists(raw, expected):
    assert parse_terms(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "ASAP",
        "Flexible",
        "Summer",  # no year
        "2027",  # no season
        "Summer 1999",
        "Summer 2027 at Google",  # unknown word
        "Q3 2027",
        "May 2027 - Dec 2028",  # 20 months, longer than any internship
        "Fall 2027 - Winter 2027",  # explicit years running backwards
        "Summer 20277",
    ],
)
def test_parse_terms_rejects_unreadable(raw):
    with pytest.raises(ValueError):
        parse_terms(raw)


def test_normalize_term_rejects_multi_term_span():
    with pytest.raises(ValueError, match="normalize_terms"):
        normalize_term("May-Dec 2027")


def test_normalize_terms_dedupes_sorts_and_drops_unstated():
    raw = ["Fall 2027", "summer '27", "S27", "May-Dec 2027", "Winter 2028", "N/A", "  "]
    assert normalize_terms(raw) == ["Summer 2027", "Fall 2027", "Winter 2028"]
    assert normalize_terms([]) == []
    assert normalize_terms(None) == []
    assert normalize_terms("Summer 2027") == ["Summer 2027"]


def test_term_sort_key_is_chronological():
    terms = ["Fall 2026", "Winter 2027", "Summer 2027", "Fall 2027", "Winter 2028"]
    assert sorted(reversed(terms), key=term_sort_key) == terms


def test_canonical_terms_are_fixed_points():
    for term in ["Winter 2027", "Summer 2027", "Fall 2027"]:
        assert normalize_term(term) == term


# --- locations -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Toronto, ON", "Toronto, ON"),
        ("Toronto, Ontario, Canada", "Toronto, ON"),
        ("Toronto, Ontario", "Toronto, ON"),
        ("Toronto ON", "Toronto, ON"),
        ("toronto on", "Toronto, ON"),
        ("Toronto Ontario", "Toronto, ON"),
        ("toronto", "Toronto, ON"),
        ("TORONTO", "Toronto, ON"),
        ("Toronto, Canada", "Toronto, ON"),
        ("Toronto, CA", "Toronto, ON"),  # bare CA after a known Canadian city means Canada
        ("Toronto, ON, CA", "Toronto, ON"),
        ("Toronto, ON (Hybrid)", "Toronto, ON"),
        ("Hybrid - Toronto", "Toronto, ON"),
        ("Waterloo, ON, Canada", "Waterloo, ON"),
        ("Kitchener", "Kitchener, ON"),
        ("Montréal, Québec", "Montreal, QC"),
        ("Montreal", "Montreal, QC"),
        ("montreal, qc", "Montreal, QC"),
        ("Québec City", "Quebec City, QC"),
        ("Vancouver, British Columbia, Canada", "Vancouver, BC"),
        ("Burnaby, BC", "Burnaby, BC"),
        ("Calgary, Alberta", "Calgary, AB"),
        ("Halifax, Nova Scotia", "Halifax, NS"),
        ("St. John’s, Newfoundland and Labrador", "St. John's, NL"),
        ("st johns", "St. John's, NL"),
        ("Ottawa", "Ottawa, ON"),
        ("London, Ontario", "London, ON"),  # not in the city table, but the province is known
        ("Brampton, Canada", "Brampton, Canada"),
        ("Ontario", "Ontario, Canada"),
        ("Ontario, Canada", "Ontario, Canada"),
        ("Canada", "Canada"),
        ("San Francisco, CA", "San Francisco, CA"),  # bare CA otherwise means California
        ("San Francisco, California, United States", "San Francisco, CA"),
        ("SF", "San Francisco, CA"),
        ("NYC", "New York, NY"),
        ("New York, New York", "New York, NY"),
        ("Seattle, Washington", "Seattle, WA"),
        ("Austin, TX, USA", "Austin, TX"),
        ("Salt Lake City UT", "Salt Lake City, UT"),
        ("United States", "United States"),
        ("USA", "United States"),
        ("London, UK", "London, UK"),
        ("london, united kingdom", "London, United Kingdom"),
        ("London, England, United Kingdom", "London, United Kingdom"),
        ("Berlin, Germany", "Berlin, Germany"),
        ("Bangalore", "Bangalore"),
    ],
)
def test_normalize_location(raw, expected):
    assert normalize_location(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Remote", "Remote"),
        ("remote", "Remote"),
        ("Fully Remote", "Remote"),
        ("Remote - Canada", "Remote (Canada)"),
        ("Remote (Canada)", "Remote (Canada)"),
        ("Canada (Remote)", "Remote (Canada)"),
        ("Remote, Canada", "Remote (Canada)"),
        ("Remote in Canada", "Remote (Canada)"),
        ("Canada - Remote", "Remote (Canada)"),
        ("Remote - Ontario", "Remote (Canada)"),
        ("Toronto, ON (Remote)", "Remote (Canada)"),
        ("Remote (US or Canada)", "Remote (Canada)"),  # Canada wins: eligibility is what matters
        ("Remote US", "Remote (US)"),
        ("Remote - USA", "Remote (US)"),
        ("US Remote", "Remote (US)"),
        ("Remote (United States)", "Remote (US)"),
        ("Remote - California", "Remote (US)"),
        ("Remote - North America", "Remote (North America)"),
        ("remote - europe", "Remote (Europe)"),
    ],
)
def test_normalize_location_remote_variants(raw, expected):
    assert normalize_location(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "N/A", "unknown", "12345", "---", "x" * 200])
def test_normalize_location_rejects_junk(raw):
    with pytest.raises(ValueError):
        normalize_location(raw)


@pytest.mark.parametrize(
    "canonical",
    [
        "Toronto, ON", "Montreal, QC", "Quebec City, QC", "St. John's, NL", "Ontario, Canada", "Canada",
        "San Francisco, CA", "New York, NY", "Seattle, WA", "United States", "London, United Kingdom",
        "Remote", "Remote (Canada)", "Remote (US)", "Remote (Europe)", "Brampton, Canada",
    ],
)  # fmt: skip
def test_canonical_locations_are_fixed_points(canonical):
    assert normalize_location(canonical) == canonical


def test_normalize_locations_dedupes_keeping_order():
    raw = ["Toronto, Ontario, Canada", "Remote - Canada", "toronto", "Waterloo, ON", "Canada (Remote)", "", "TBD"]
    assert normalize_locations(raw) == ["Toronto, ON", "Remote (Canada)", "Waterloo, ON"]
    assert normalize_locations(None) == []
    assert normalize_locations("Toronto") == ["Toronto, ON"]


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Toronto, ON", True), ("Ontario, Canada", True), ("Canada", True), ("Remote (Canada)", True),
        ("Brampton, Canada", True), ("Seattle, WA", False), ("Remote (US)", False), ("Remote", False),
        ("London, United Kingdom", False),
    ],
)  # fmt: skip
def test_is_canadian_location(location, expected):
    assert is_canadian_location(location) is expected


# --- role type -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("swe", RoleType.swe),
        ("SWE", RoleType.swe),
        ("ml_ai", RoleType.ml_ai),
        ("ML_AI", RoleType.ml_ai),
        ("ml/ai", RoleType.ml_ai),
        ("ml", RoleType.ml_ai),
        ("AI", RoleType.ml_ai),
        ("Machine Learning", RoleType.ml_ai),
        ("AI/ML", RoleType.ml_ai),
        ("backend", RoleType.swe),
        ("Full Stack", RoleType.swe),
        ("full-stack", RoleType.swe),
        ("Software", RoleType.swe),
        ("DevOps", RoleType.infra),
        ("SRE", RoleType.infra),
        ("platform", RoleType.infra),
        ("Data Science", RoleType.data),
        ("front-end", RoleType.frontend),
        ("iOS", RoleType.mobile),
        ("embedded", RoleType.hardware),
        ("UX", RoleType.design),
        ("Product Manager", RoleType.product),
        ("Research Scientist", RoleType.research),
        ("cybersecurity", RoleType.security),
        ("quantitative trading", RoleType.other),  # unknown non-empty -> other
        ("other", RoleType.other),
        (RoleType.data, RoleType.data),
    ],
)
def test_normalize_role_type(raw, expected):
    assert normalize_role_type(raw) is expected


@pytest.mark.parametrize("raw", ["", "   "])
def test_normalize_role_type_rejects_empty(raw):
    with pytest.raises(ValueError):
        normalize_role_type(raw)


# --- tech stack ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["Python", " python ", "PY"], ["python"]),
        (["Golang", "go"], ["go"]),
        (["JS", "TS", "React.js", "Node"], ["javascript", "typescript", "react", "node.js"]),
        (["k8s", "Kubernetes", "Docker"], ["kubernetes", "docker"]),
        (["Postgres", "PostgreSQL", "psql"], ["postgresql"]),
        (["C/C++", "cpp", "C#"], ["c", "c++", "c#"]),
        (["Amazon Web Services", "AWS", "Google Cloud"], ["aws", "gcp"]),
        (["", "  ", "Rust"], ["rust"]),
        (["Apache   Kafka"], ["apache kafka"]),
        ("python, go, , k8s", ["python", "go", "kubernetes"]),
        ([], []),
        (None, []),
    ],
)
def test_normalize_tech_stack(raw, expected):
    assert normalize_tech_stack(raw) == expected


def test_normalize_tech_stack_caps_length():
    raw = [f"tool{i}" for i in range(40)]
    out = normalize_tech_stack(raw)
    assert len(out) == MAX_TECH_STACK
    assert out == raw[:MAX_TECH_STACK]


# --- the model -------------------------------------------------------------


def test_model_validate_sloppy_llm_json():
    blob = json.dumps(
        {
            "is_internship": True,
            "terms": ["summer '27", "Fall/Autumn 2027", "May–Dec 2027", "not specified"],
            "locations": ["Toronto, Ontario, Canada", "Remote - Canada", "toronto", "Montréal"],
            "remote_ok": True,
            "canada_eligible": True,
            "role_type": "Machine Learning",
            "tech_stack": ["Python", "PyTorch", "k8s", "python", "Golang", ""],
        }
    )
    ex = PostingExtraction.model_validate_json(blob)
    assert ex.is_internship is True
    assert ex.terms == ["Summer 2027", "Fall 2027"]
    assert ex.locations == ["Toronto, ON", "Remote (Canada)", "Montreal, QC"]
    assert ex.remote_ok is True and ex.canada_eligible is True
    assert ex.role_type is RoleType.ml_ai
    assert ex.tech_stack == ["python", "pytorch", "kubernetes", "go"]
    # Canonical output round-trips unchanged.
    assert PostingExtraction.model_validate(ex.model_dump()) == ex
    assert ex.model_dump(mode="json")["role_type"] == "ml_ai"


def test_model_accepts_empty_lists_as_not_stated():
    ex = PostingExtraction.model_validate(valid_payload(terms=[], locations=[], tech_stack=[], role_type="other"))
    assert ex.terms == [] and ex.locations == [] and ex.tech_stack == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"terms": ["Summer 2027", "ASAP"]},
        {"terms": ["Summer"]},
        {"terms": [2027]},
        {"locations": ["12345"]},
        {"role_type": ""},
        {"role_type": None},
        {"is_internship": "maybe"},
        {"tech_stack": [3]},
    ],
)
def test_model_rejects_what_cannot_be_normalized(overrides):
    with pytest.raises(ValidationError):
        PostingExtraction.model_validate(valid_payload(**overrides))


@pytest.mark.parametrize(
    "missing", ["is_internship", "terms", "locations", "remote_ok", "canada_eligible", "role_type", "tech_stack"]
)
def test_model_requires_every_field(missing):
    payload = valid_payload()
    del payload[missing]
    with pytest.raises(ValidationError):
        PostingExtraction.model_validate(payload)


def test_openai_json_schema_is_strict_mode_compatible():
    block = PostingExtraction.openai_json_schema()
    assert block["name"] == "posting_extraction"
    assert block["strict"] is True
    schema = block["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"]) == set(PostingExtraction.model_fields)
    assert schema["properties"]["role_type"]["enum"] == [r.value for r in RoleType]
    assert schema["properties"]["terms"] == {
        "description": PostingExtraction.model_fields["terms"].description,
        "type": "array",
        "items": {"type": "string"},
    }

    text = json.dumps(block)
    for keyword in ('"$ref"', '"$defs"', '"default"', '"title"', '"allOf"', '"anyOf"'):
        assert keyword not in text

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    for name, prop in schema["properties"].items():
        assert prop.get("description"), f"{name} has no description for the LLM"
