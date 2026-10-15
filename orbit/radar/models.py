"""Pydantic models shared across the radar pipeline.

`NormalizedPosting` is what every source adapter returns: one shape regardless of job board.
`PostingExtraction` holds the LLM-extracted fields; its validators turn sloppy model output into
canonical values (see the `normalize_*` helpers, which eval and the match filters reuse).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def to_naive_local(dt: datetime | None) -> datetime | None:
    """Orbit stores `timestamp without time zone` in local time; drop microseconds like the DB defaults."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.replace(microsecond=0)


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (with or without offset, 'Z' allowed) into naive local time."""
    if not value:
        return None
    try:
        return to_naive_local(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


class NormalizedPosting(BaseModel):
    """One job posting as fetched from a public job-board API, before any LLM work."""

    source: str
    external_id: str
    company_slug: str
    title: str
    url: str
    location_raw: str = ""
    department: str | None = None
    description_text: str = ""
    posted_at: datetime | None = None
    updated_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("posted_at", "updated_at")
    @classmethod
    def _naive_local(cls, v: datetime | None) -> datetime | None:
        return to_naive_local(v)

    def content_hash(self) -> str:
        """sha256 over the fields that matter for re-extraction (not raw, not timestamps).

        A change here resets extraction and embedding to pending (M3.1); bumping the key set
        or format invalidates every stored hash, so change it deliberately.
        """
        payload = {
            "title": self.title.strip(),
            "location_raw": self.location_raw.strip(),
            "department": (self.department or "").strip(),
            "description_text": self.description_text.strip(),
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# LLM extraction (M4.1)
# ---------------------------------------------------------------------------

# Values the model (or a label file) uses for "not stated". They are dropped from list fields,
# because an empty list already means "not stated"; normalize_term/normalize_location reject them.
_UNSTATED = {"", "n/a", "na", "none", "null", "unknown", "not specified", "not stated", "unspecified", "tbd", "tba"}


def _is_unstated(value: str) -> bool:
    return re.sub(r"\s+", " ", value.strip().lower().strip(".")) in _UNSTATED


def _straighten(value: str) -> str:
    return value.replace("’", "'").replace("‘", "'").replace("`", "'")


def _ascii_key(value: str) -> str:
    """Lookup key: accents stripped, lowercased, curly quotes straightened, dots dropped, spaces collapsed."""
    value = unicodedata.normalize("NFKD", _straighten(value)).encode("ascii", "ignore").decode("ascii")
    value = value.lower().replace(".", " ")
    return re.sub(r"\s+", " ", value).strip()


# --- terms -----------------------------------------------------------------
#
# Canonical term: "<Season> <YYYY>" with Season in Winter | Summer | Fall, following Waterloo co-op
# terms: Jan-Apr = Winter, May-Aug = Summer, Sep-Dec = Fall. "Spring" is an alias of Summer (Waterloo
# calls May-Aug the spring term), so "Spring 2027" -> "Summer 2027". "Autumn" is an alias of Fall.

SEASONS = ("Winter", "Summer", "Fall")
_SEASON_WORDS = {"winter": 0, "spring": 1, "summer": 1, "fall": 2, "autumn": 2}
_MONTH_WORDS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}  # fmt: skip
_RANGE_WORDS = {"-", "to", "through", "thru", "until", "till"}
_LIST_WORDS = {"/", ",", "&", "+", "and", "or"}
_TERM_FILLERS = {
    "term", "terms", "coop", "co", "op", "internship", "internships", "intern", "interns", "program",
    "month", "months", "mo", "mos", "week", "weeks", "wk", "wks", "from", "starting", "start", "starts",
    "beginning", "of", "in", "the", "a", "semester", "session", "year", "only", "st", "nd", "rd", "th",
    "full", "time", "part", "student", "students", "early", "mid", "late",
}  # fmt: skip
_COMPACT_TERM = re.compile(r"\b([wsf])\s?'?(\d{2}|20\d{2})\b")
_COMPACT_SEASON = {"w": "winter", "s": "summer", "f": "fall"}
MAX_TERM_SPAN = 4  # a range may cover at most 4 terms (16 months, the longest Canadian internships)


def _term_name(index: int) -> str:
    return f"{SEASONS[index % 3]} {index // 3}"


def term_sort_key(term: str) -> int:
    """Chronological key for a canonical term ("Winter 2027" < "Summer 2027" < "Fall 2027")."""
    season, year = term.split(" ")
    return int(year) * 3 + SEASONS.index(season)


def parse_terms(value: str) -> list[str]:
    """Expand one free-text term description into canonical terms, sorted and deduped.

    Handles "summer 2027", "Summer '27", "S27", "Fall/Autumn 2026", "May 2027", "Winter 2027 (4 months)",
    "2027 Summer" and ranges: "May-Dec 2027" -> ["Summer 2027", "Fall 2027"]. Raises ValueError for
    anything it cannot read (no season or month, no year, unknown words, ranges over 16 months).
    """
    if not isinstance(value, str):
        raise ValueError(f"term must be a string, got {type(value).__name__}")
    text = _straighten(value).lower()
    text = re.sub(r"[‐-―−]", "-", text).replace("co-op", "coop")
    text = _COMPACT_TERM.sub(lambda m: f"{_COMPACT_SEASON[m.group(1)]} '{m.group(2)[-2:]}", text)
    tokens = re.findall(r"[a-z]+|'?\d+|[-/,&+]", text)

    # Each item is one season or month mention: its season (0..2), its year once known, whether a range
    # word joins it to the previous item, and which year token assigned its year (for "Sep - Apr 2027").
    items: list[dict[str, Any]] = []
    pending_range = False
    last_year: int | None = None
    batch = 0
    prev: str | None = None
    for tok in tokens:
        if tok in _SEASON_WORDS or tok in _MONTH_WORDS:
            season = _SEASON_WORDS[tok] if tok in _SEASON_WORDS else (_MONTH_WORDS[tok] - 1) // 4
            items.append({"season": season, "year": None, "range": pending_range and bool(items), "batch": None})
            pending_range = False
        elif tok[0] in "'0123456789":
            digits = tok.lstrip("'")
            year: int | None = None
            if tok.startswith("'") and len(digits) == 2:
                year = 2000 + int(digits)
            elif len(digits) == 4:
                if not 2000 <= int(digits) <= 2099:
                    raise ValueError(f"cannot normalize term {value!r}: implausible year {digits}")
                year = int(digits)
            elif len(digits) == 2 and prev in _SEASON_WORDS:
                year = 2000 + int(digits)
            elif len(digits) > 2:
                raise ValueError(f"cannot normalize term {value!r}: unreadable number {tok}")
            # other 1-2 digit numbers are counts or days ("4 months", "May 15") and are ignored
            if year is not None:
                batch += 1
                for item in reversed(items):
                    if item["year"] is not None:
                        break
                    item["year"], item["batch"] = year, batch
                last_year = year
        elif tok in _RANGE_WORDS:
            pending_range = True
        elif tok in _LIST_WORDS or tok in _TERM_FILLERS:
            pending_range = False
        else:
            raise ValueError(f"cannot normalize term {value!r}: unknown word {tok!r}")
        prev = tok

    if not items:
        raise ValueError(f"cannot normalize term {value!r}: no season or month")
    for item in items:  # "2027 Summer", "Summer 2027 or Fall": trailing units take the last year seen
        if item["year"] is None:
            if last_year is None:
                raise ValueError(f"cannot normalize term {value!r}: no year")
            item["year"], item["batch"] = last_year, -1

    points = [item["year"] * 3 + item["season"] for item in items]
    for i, item in enumerate(items):
        if item["range"] and points[i - 1] > points[i] and items[i - 1]["batch"] == item["batch"]:
            points[i - 1] -= 3  # "Sep - Apr 2027": the start month belongs to the year before
    indexes: set[int] = set()
    for i, item in enumerate(items):
        end = points[i]
        if not item["range"]:
            indexes.add(end)
            continue
        start = points[i - 1]
        if start > end:
            raise ValueError(f"cannot normalize term {value!r}: range runs backwards")
        if end - start + 1 > MAX_TERM_SPAN:
            raise ValueError(f"cannot normalize term {value!r}: range longer than {MAX_TERM_SPAN} terms")
        indexes.update(range(start, end + 1))
    return [_term_name(i) for i in sorted(indexes)]


def normalize_term(value: str) -> str:
    """Normalize text that names exactly one term ("Summer '27" -> "Summer 2027"); ValueError otherwise."""
    terms = parse_terms(value)
    if len(terms) != 1:
        raise ValueError(f"{value!r} covers {len(terms)} terms {terms}; use normalize_terms")
    return terms[0]


def normalize_terms(values: Iterable[str] | str | None) -> list[str]:
    """Normalize a list of term strings: expand spans, drop "not stated" markers, dedupe, sort chronologically."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: set[str] = set()
    for v in values:
        if isinstance(v, str) and _is_unstated(v):
            continue
        out.update(parse_terms(v))
    return sorted(out, key=term_sort_key)


# --- locations -------------------------------------------------------------
#
# Canonical forms: Canadian cities "City, XX" (province code); US cities "City, ST"; elsewhere
# "City, Country" (tidied passthrough); a lone province "Ontario, Canada"; a lone country "Canada" /
# "United States"; remote roles "Remote", "Remote (Canada)", "Remote (US)" or "Remote (<Region>)".

PROVINCES = {
    "ON": "Ontario", "BC": "British Columbia", "QC": "Quebec", "AB": "Alberta", "MB": "Manitoba",
    "SK": "Saskatchewan", "NS": "Nova Scotia", "NB": "New Brunswick", "NL": "Newfoundland and Labrador",
    "PE": "Prince Edward Island", "YT": "Yukon", "NT": "Northwest Territories", "NU": "Nunavut",
}  # fmt: skip
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}  # fmt: skip
_PROVINCE_KEYS = {_ascii_key(name): code for code, name in PROVINCES.items()} | {
    "newfoundland": "NL", "pei": "PE", "ont": "ON", "pq": "QC", "yukon territory": "YT",
}  # fmt: skip
_STATE_KEYS = {_ascii_key(name): code for code, name in US_STATES.items()} | {"washington dc": "DC"}
_CANADA_KEYS = {"canada", "ca", "can", "canadian"}
_US_KEYS = {"us", "usa", "u s", "u s a", "united states", "united states of america"}

# Major Canadian tech cities: alias key -> (canonical city, province code).
_CA_CITIES: dict[str, tuple[str, str]] = {}
for _names, _prov in [
    (("Toronto",), "ON"), (("Waterloo",), "ON"), (("Kitchener",), "ON"), (("Ottawa",), "ON"),
    (("Mississauga",), "ON"), (("Markham",), "ON"), (("Hamilton",), "ON"), (("Guelph",), "ON"),
    (("Montreal",), "QC"), (("Quebec City", "Ville de Quebec"), "QC"), (("Vancouver",), "BC"),
    (("Victoria",), "BC"), (("Burnaby",), "BC"), (("Calgary",), "AB"), (("Edmonton",), "AB"),
    (("Winnipeg",), "MB"), (("Saskatoon",), "SK"), (("Regina",), "SK"), (("Halifax",), "NS"),
    (("Fredericton",), "NB"), (("St. John's", "St Johns", "Saint John's"), "NL"),
]:  # fmt: skip
    for _alias in _names:
        _CA_CITIES[_ascii_key(_alias)] = (_names[0], _prov)
# A few US tech hubs so "SF", "NYC" or "Seattle" come out as "City, ST".
_US_CITIES: dict[str, tuple[str, str]] = {}
for _names, _st in [
    (("San Francisco", "SF"), "CA"), (("New York", "New York City", "NYC"), "NY"), (("Seattle",), "WA"),
    (("Boston",), "MA"), (("Austin",), "TX"), (("Chicago",), "IL"), (("Los Angeles",), "CA"),
    (("Mountain View",), "CA"), (("Palo Alto",), "CA"), (("Menlo Park",), "CA"), (("Sunnyvale",), "CA"),
    (("San Jose",), "CA"), (("Redmond",), "WA"),
]:  # fmt: skip
    for _alias in _names:
        _US_CITIES[_ascii_key(_alias)] = (_names[0], _st)

_REMOTE_RE = re.compile(r"\b(remote|wfh|work from home)\b", re.IGNORECASE)
_WORKPLACE_RE = re.compile(r"\b(hybrid|on-?site|in[- ]office|in[- ]person)\b", re.IGNORECASE)
_REMOTE_FILLERS = re.compile(r"\b(only|based|friendly|first|fully|eligible|within|in|anywhere|from)\b|100%", re.I)
_EDGE_PUNCT = " \t-|:/,;()[]"
MAX_LOCATION_LENGTH = 120


def _tidy(part: str) -> str:
    """Collapse whitespace; capitalize words of all-lower or long all-caps text, otherwise keep the casing."""
    part = re.sub(r"\s+", " ", part).strip(_EDGE_PUNCT + ".")
    if part.islower() or (part.isupper() and len(part) > 3):
        part = re.sub(r"(^|[\s\-])([a-z])", lambda m: m.group(1) + m.group(2).upper(), part.lower())
    return part


def _ca_city(city: str) -> tuple[str, str] | None:
    return _CA_CITIES.get(_ascii_key(city))


def _us_city(city: str) -> tuple[str, str] | None:
    return _US_CITIES.get(_ascii_key(city))


def _region_code(region: str) -> tuple[str, str] | None:
    """("CA", province) or ("US", state) for a full name or a 2-letter code. Bare "CA" reads as California."""
    key = _ascii_key(region)
    if key in _PROVINCE_KEYS:
        return "CA", _PROVINCE_KEYS[key]
    if key in _STATE_KEYS:
        return "US", _STATE_KEYS[key]
    code = key.replace(" ", "").upper()
    if len(code) == 2 and code in PROVINCES:
        return "CA", code
    if len(code) == 2 and code in US_STATES:
        return "US", code
    return None


def _country(part: str) -> str | None:
    key = _ascii_key(part)
    if key in _CANADA_KEYS:
        return "CA"
    if key in _US_KEYS:
        return "US"
    return None


def _normalize_remote(text: str) -> str:
    """Remote location -> "Remote", "Remote (Canada)", "Remote (US)" or "Remote (<Region>)".

    Any Canadian mention wins ("Remote (US or Canada)" -> "Remote (Canada)"), since what matters
    downstream is whether a student in Canada can take the role. A city is dropped: "Toronto (Remote)"
    becomes "Remote (Canada)".
    """
    rest = _REMOTE_FILLERS.sub(" ", _REMOTE_RE.sub(" ", text))
    rest = re.sub(r"\s+", " ", re.sub(r"[()\[\]]", " ", rest)).strip(_EDGE_PUNCT + ".")
    if not rest:
        return "Remote"
    pieces = [p.strip() for p in re.split(r"[,/;|&]|\s-\s|\bor\b|\band\b", rest) if p.strip()]
    words = {_ascii_key(w) for w in re.split(r"[\s,/;|&()-]+", rest) if w}

    def canadian(piece: str) -> bool:
        region = _region_code(piece) if (len(piece) > 2 or piece.isupper()) else None
        return bool(_ca_city(piece) or (region and region[0] == "CA") or _country(piece) == "CA")

    def american(piece: str) -> bool:
        region = _region_code(piece) if (len(piece) > 2 or piece.isupper()) else None
        return bool(_us_city(piece) or (region and region[0] == "US") or _country(piece) == "US")

    if words & {"canada", "canadian"} or any(canadian(p) for p in pieces):
        return "Remote (Canada)"
    if words & {"us", "usa"} or any(american(p) for p in pieces):
        return "Remote (US)"
    return f"Remote ({_tidy(rest)})"


def normalize_location(value: str) -> str:
    """Normalize one location string to its canonical form; ValueError for empty or non-location junk.

    "Toronto, Ontario, Canada" / "Toronto ON" / "toronto" -> "Toronto, ON"; "Montréal" -> "Montreal, QC";
    "Remote - Canada" / "Canada (Remote)" -> "Remote (Canada)"; "Remote US" -> "Remote (US)";
    "Seattle, Washington" -> "Seattle, WA"; "London, UK" stays "London, UK". Junk means no letters at
    all or longer than 120 characters; any other text passes through tidied, as "City" or "City, Country".
    """
    if not isinstance(value, str):
        raise ValueError(f"location must be a string, got {type(value).__name__}")
    text = re.sub(r"\s+", " ", _straighten(value)).strip()
    if _is_unstated(text):
        raise ValueError(f"empty or unstated location {value!r}")
    if len(text) > MAX_LOCATION_LENGTH or not re.search(r"[^\W\d_]", text):
        raise ValueError(f"not a location: {value!r}")
    if _REMOTE_RE.search(text):
        return _normalize_remote(text)

    text = re.sub(r"\(\s*\)|\[\s*\]", " ", _WORKPLACE_RE.sub(" ", text))
    parts = [p for p in (_tidy(p) for p in text.split(",")) if p]
    if not parts:
        raise ValueError(f"not a location: {value!r}")

    # "Toronto ON" / "Toronto Ontario": split off a trailing uppercase region code or a full region name.
    if len(parts) == 1 and " " in parts[0]:
        head, last = parts[0].rsplit(" ", 1)
        key = _ascii_key(parts[0])
        if len(last) == 2 and (last.isupper() or _ca_city(head) or _us_city(head)) and _region_code(last):
            parts = [head, last]
        else:
            for name in sorted([*_PROVINCE_KEYS, *_STATE_KEYS], key=len, reverse=True):
                if key.endswith(" " + name) and not (_ca_city(parts[0]) or _us_city(parts[0])):
                    parts = [parts[0][: len(parts[0]) - len(name)].strip(), name]
                    break

    # A trailing country. Bare "CA" as the second of two parts is a region (California, or Canada for a
    # known Canadian city), handled below; as the third part ("Toronto, ON, CA") it is the country.
    country: str | None = None
    if len(parts) >= 2 and _country(parts[-1]) and (len(parts) >= 3 or parts[-1].upper() != "CA"):
        country = _country(parts[-1])
        parts = parts[:-1]

    city = parts[0]
    ca, us = _ca_city(city), _us_city(city)
    if len(parts) == 1:
        if country is None and _country(city) and (len(city) > 2 or city.isupper()):
            return "Canada" if _country(city) == "CA" else "United States"
        region = _region_code(city) if (len(city) > 2 or city.isupper()) else None
        if region and not ca and not us:  # a lone province or state
            if region[0] == "CA":
                return f"{PROVINCES[region[1]]}, Canada"
            return f"{US_STATES[region[1]]}, United States"
        if ca and country != "US":
            return f"{ca[0]}, {ca[1]}"
        if us and country != "CA":
            return f"{us[0]}, {us[1]}"
        if country:
            return f"{city}, {'Canada' if country == 'CA' else 'United States'}"
        return city

    region = _region_code(parts[1])
    if region and parts[1].strip().upper() == "CA" and ca:
        region = ("CA", ca[1])  # "Toronto, CA" is Canada; "San Francisco, CA" is California
    if region:
        if region[0] == "CA" and ca and ca[1] == region[1]:
            return f"{ca[0]}, {ca[1]}"
        if region[0] == "US" and us and us[1] == region[1]:
            return f"{us[0]}, {us[1]}"
        return f"{city}, {region[1]}"
    if country == "CA" and ca:
        return f"{ca[0]}, {ca[1]}"
    if country == "US" and us:
        return f"{us[0]}, {us[1]}"
    # "London, England, United Kingdom" -> "London, United Kingdom"; "Berlin, Germany" stays.
    return f"{city}, {parts[-1]}"


def normalize_locations(values: Iterable[str] | str | None) -> list[str]:
    """Normalize a list of locations: drop "not stated" markers, dedupe, keep order."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for v in values:
        if isinstance(v, str) and _is_unstated(v):
            continue
        loc = normalize_location(v)
        if loc not in out:
            out.append(loc)
    return out


def is_canadian_location(location: str) -> bool:
    """True for a canonical location in Canada ("Toronto, ON", "Ontario, Canada", "Canada", "Remote (Canada)")."""
    return (
        location in ("Canada", "Remote (Canada)")
        or location.endswith(", Canada")
        or location.rsplit(", ", 1)[-1] in PROVINCES
    )


# --- role type and tech stack ---------------------------------------------


class RoleType(StrEnum):
    swe = "swe"
    ml_ai = "ml_ai"
    data = "data"
    infra = "infra"
    security = "security"
    mobile = "mobile"
    frontend = "frontend"
    hardware = "hardware"
    product = "product"
    design = "design"
    research = "research"
    other = "other"


_ROLE_ALIASES: dict[str, RoleType] = {}
for _role, _aliases in {
    RoleType.swe: ("software", "software engineer", "software engineering", "software developer", "sde",
                   "backend", "back end", "full stack", "fullstack", "developer", "web", "web developer",
                   "engineering", "generalist"),
    RoleType.ml_ai: ("ml", "ai", "machine learning", "artificial intelligence", "ml ai", "ai ml", "mle",
                     "ml engineer", "machine learning engineer", "deep learning", "llm", "nlp", "computer vision"),
    RoleType.data: ("data science", "data scientist", "data engineer", "data engineering", "analytics",
                    "data analyst", "data analytics", "business intelligence", "bi"),
    RoleType.infra: ("infrastructure", "devops", "dev ops", "sre", "site reliability", "site reliability engineering",
                     "platform", "platform engineering", "cloud", "production engineering"),
    RoleType.security: ("cybersecurity", "cyber security", "appsec", "infosec", "application security"),
    RoleType.mobile: ("ios", "android", "mobile developer"),
    RoleType.frontend: ("front end", "ui engineer", "web frontend", "frontend engineer"),
    RoleType.hardware: ("embedded", "firmware", "electrical", "asic", "fpga", "hardware engineer"),
    RoleType.product: ("product manager", "product management", "pm", "apm", "associate product manager"),
    RoleType.design: ("ux", "ui", "ux design", "ui ux", "ux ui", "product design", "product designer", "designer"),
    RoleType.research: ("research scientist", "researcher", "research engineer"),
}.items():  # fmt: skip
    for _alias in _aliases:
        _ROLE_ALIASES[_alias] = _role


def normalize_role_type(value: str | RoleType) -> RoleType:
    """Case-insensitive role type with aliases ("Machine Learning" -> ml_ai, "DevOps" -> infra).

    An unrecognized non-empty string maps to `other` (strict-mode output can only be a valid enum value
    anyway, so this mostly matters for hand-written labels); an empty string raises ValueError.
    """
    if isinstance(value, RoleType):
        return value
    if not isinstance(value, str):
        raise ValueError(f"role_type must be a string, got {type(value).__name__}")
    key = re.sub(r"[\s_\-/&]+", " ", value.strip().lower()).strip()
    if not key:
        raise ValueError("role_type is empty")
    if key.replace(" ", "_") in RoleType.__members__:
        return RoleType(key.replace(" ", "_"))
    return _ROLE_ALIASES.get(key, RoleType.other)


_TECH_ALIASES: dict[str, tuple[str, ...]] = {
    "golang": ("go",), "js": ("javascript",), "ecmascript": ("javascript",), "es6": ("javascript",),
    "ts": ("typescript",), "k8s": ("kubernetes",), "k8": ("kubernetes",), "postgres": ("postgresql",),
    "postgre": ("postgresql",), "psql": ("postgresql",), "py": ("python",), "python3": ("python",),
    "node": ("node.js",), "nodejs": ("node.js",), "node js": ("node.js",), "reactjs": ("react",),
    "react.js": ("react",), "vuejs": ("vue",), "vue.js": ("vue",), "nextjs": ("next.js",),
    "angularjs": ("angular",), "c sharp": ("c#",), "csharp": ("c#",), "cpp": ("c++",), "c/c++": ("c", "c++"),
    "objective c": ("objective-c",), "objc": ("objective-c",), "amazon web services": ("aws",),
    "google cloud": ("gcp",), "google cloud platform": ("gcp",), "microsoft azure": ("azure",),
    "sklearn": ("scikit-learn",), "scikit learn": ("scikit-learn",), "torch": ("pytorch",),
    "mongo": ("mongodb",), "graph ql": ("graphql",), "restful": ("rest",), "rest api": ("rest",),
    "rest apis": ("rest",), "html5": ("html",), "css3": ("css",), "mssql": ("sql server",),
    "rustlang": ("rust",), "dotnet": (".net",), "llms": ("llm",),
}  # fmt: skip
MAX_TECH_STACK = 25


def normalize_tech_stack(values: Iterable[str] | str | None) -> list[str]:
    """Lowercase, strip, canonicalize aliases ("golang" -> "go", "k8s" -> "kubernetes"), dedupe in order, cap at 25.

    A single string is split on commas. Empty items are dropped; items past the cap are dropped silently.
    """
    if values is None:
        return []
    if isinstance(values, str):
        values = values.split(",")
    out: list[str] = []
    for v in values:
        if not isinstance(v, str):
            raise ValueError(f"tech_stack items must be strings, got {type(v).__name__}")
        key = re.sub(r"\s+", " ", v.strip().lower())
        for item in _TECH_ALIASES.get(key, (key,)) if key else ():
            if item not in out:
                out.append(item)
    return out[:MAX_TECH_STACK]


# --- the model -------------------------------------------------------------


class PostingExtraction(BaseModel):
    """Structured fields extracted from one job posting. Use only what the posting says; do not guess."""

    is_internship: bool = Field(
        description=(
            "True if the role is an internship, co-op, or student/intern program (including PEY and "
            "student research positions). False for new-grad, entry-level, full-time, part-time or contract "
            "roles that are not aimed at current students."
        )
    )
    terms: list[str] = Field(
        description=(
            'Work terms the role covers, each formatted exactly "<Season> <YYYY>" with Season one of Winter '
            '(Jan-Apr), Summer (May-Aug) or Fall (Sep-Dec), e.g. "Summer 2027". Use one entry per term: an '
            '8-month May-Dec 2027 role is ["Summer 2027", "Fall 2027"]. Map start dates to the term containing '
            "them. Empty list if the posting states no term or dates."
        )
    )
    locations: list[str] = Field(
        description=(
            'Work locations. Canadian cities as "City, XX" with the 2-letter province code (e.g. "Toronto, ON", '
            '"Montreal, QC"); US cities as "City, ST" (e.g. "Seattle, WA"); elsewhere "City, Country". Remote '
            'roles as "Remote (Canada)", "Remote (US)", or "Remote" if no region is given. Empty list if none '
            "are stated."
        )
    )
    remote_ok: bool = Field(description="True if the role can be done fully remotely; false for on-site or hybrid.")
    canada_eligible: bool = Field(
        description=(
            "True if a student living in Canada could take this role: it has a Canadian location or is remote "
            "from Canada. False if it is only in other countries or requires US work authorization or residency."
        )
    )
    role_type: RoleType = Field(
        description=(
            "Primary role category: swe (general software, backend, full stack), ml_ai (machine learning, AI, "
            "LLM), data (data science, analytics, data engineering), infra (devops, SRE, platform, cloud), "
            "security, mobile, frontend, hardware (embedded, firmware, electrical), product (product "
            "management), design (UX/UI), research (research scientist roles), other."
        )
    )
    tech_stack: list[str] = Field(
        description=(
            "Technologies the posting names (languages, frameworks, tools, clouds), lowercase, one per item, "
            'e.g. ["python", "react", "aws"]. At most 25. Empty list if none are named.'
        )
    )

    @field_validator("terms", mode="before")
    @classmethod
    def _norm_terms(cls, v: Any) -> list[str]:
        return normalize_terms(v)

    @field_validator("locations", mode="before")
    @classmethod
    def _norm_locations(cls, v: Any) -> list[str]:
        return normalize_locations(v)

    @field_validator("role_type", mode="before")
    @classmethod
    def _norm_role(cls, v: Any) -> RoleType:
        return normalize_role_type(v)

    @field_validator("tech_stack", mode="before")
    @classmethod
    def _norm_tech(cls, v: Any) -> list[str]:
        return normalize_tech_stack(v)

    @classmethod
    def openai_json_schema(cls) -> dict[str, Any]:
        """The `json_schema` block for an OpenAI `response_format` in strict mode.

        Strict mode needs every property listed in `required`, `additionalProperties: false` on every
        object and no unsupported keywords, so `$ref`s are inlined and `title`/`default` are dropped.
        """
        schema = cls.model_json_schema()
        defs = schema.pop("$defs", {})

        def fix(node: Any) -> Any:
            if isinstance(node, list):
                return [fix(x) for x in node]
            if not isinstance(node, dict):
                return node
            if "$ref" in node:
                target = copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]])
                node = target | {k: v for k, v in node.items() if k != "$ref"}
            out: dict[str, Any] = {}
            for key, val in node.items():
                if key in ("title", "default"):
                    continue
                out[key] = {k: fix(v) for k, v in val.items()} if key == "properties" else fix(val)
            if out.get("type") == "object":
                out["additionalProperties"] = False
                out["required"] = list(out.get("properties", {}))
            return out

        return {"name": "posting_extraction", "strict": True, "schema": fix(schema)}
