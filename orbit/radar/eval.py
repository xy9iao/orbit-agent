"""Extraction eval harness (M4.4): run the extractor over a labeled set and score each field.

Label file format (YAML; `evals/radar_labels.yaml` is the real set, `tests/fixtures/radar/labels_small.yaml`
the test one):

    status: "UNVERIFIED — needs human review"   # any text; "UNVERIFIED" (any case) means provisional
    version: 1
    notes: "free text"
    items:
      - id: "greenhouse:faire:1234567"            # stable, unique: <source>:<company_slug>:<external_id>
        source: greenhouse                        # greenhouse | lever | ashby
        company: "Faire"
        title: "Software Engineering Intern"
        location_raw: "Toronto, ON"
        department: "Engineering"                 # optional (null)
        url: "https://..."
        description_text: |                       # plain-text snapshot, at most 6000 chars
          ...
        expected:                                 # all seven fields, validated by PostingExtraction
          is_internship: true
          terms: ["Summer 2027"]
          locations: ["Toronto, ON"]
          remote_ok: false
          canada_eligible: true
          role_type: swe
          tech_stack: ["python", "react"]
        label_notes: "why an ambiguous field was labeled this way"   # optional

`expected` goes through the same validators as model output, so a label may be written sloppily
("summer '27", "Toronto, Ontario") and is compared in canonical form; unknown keys are rejected so a
typo cannot silently drop a field.

Scoring: every item counts for every field (n = items). Booleans and role_type are exact matches.
List fields (terms, locations, tech_stack) score exact set equality as the accuracy, plus mean Jaccard
(|A∩B| / |A∪B|, 1.0 when both are empty) as a softer secondary number. A failed extraction is wrong
for every field (Jaccard 0) and is listed in the report.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from orbit.core.router import ModelRouter
from orbit.radar.extract import TASK, ExtractionOutcome, describe_error, extract_posting
from orbit.radar.models import PostingExtraction
from orbit.storage.db import Database

DEFAULT_LABELS_PATH = Path("evals/radar_labels.yaml")
KV_LAST_EVAL = "radar:last_eval"
MAX_SNAPSHOT_CHARS = 6000
SOURCES = ("greenhouse", "lever", "ashby")
BOOL_FIELDS = ("is_internship", "remote_ok", "canada_eligible")
ENUM_FIELDS = ("role_type",)
LIST_FIELDS = ("terms", "locations", "tech_stack")
FIELDS = tuple(PostingExtraction.model_fields)  # report order follows the model
DEFAULT_CONCURRENCY = 3
# Consecutive provider/network errors after which the eval stops: an outage is not an accuracy result.
MAX_CONSECUTIVE_PROVIDER_ERRORS = 3
UNVERIFIED_BANNER = "⚠ UNVERIFIED LABELS — numbers are provisional until a human reviews evals/radar_labels.yaml"


class LabelFileError(ValueError):
    """The label file is missing, not YAML, or has a malformed item (the message names the item id)."""


class EvalAbortedError(RuntimeError):
    """Too many consecutive provider errors: the run measured the outage, not the model."""


# ---------------------------------------------------------------------------
# Label file
# ---------------------------------------------------------------------------


class LabelItem(BaseModel):
    """One labeled posting. It has the attributes extract.build_messages reads, so it is passed as-is."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    source: str
    company: str = Field(min_length=1)
    title: str = Field(min_length=1)
    location_raw: str = ""
    department: str | None = None
    url: str = ""
    description_text: str = Field(max_length=MAX_SNAPSHOT_CHARS)
    expected: PostingExtraction
    label_notes: str | None = None

    @field_validator("source")
    @classmethod
    def _known_source(cls, v: str) -> str:
        if v not in SOURCES:
            raise ValueError(f"source must be one of {', '.join(SOURCES)}, got {v!r}")
        return v

    @field_validator("location_raw", "url", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> Any:
        return "" if v is None else v

    @field_validator("expected", mode="before")
    @classmethod
    def _expected_keys(cls, v: Any) -> Any:
        if isinstance(v, dict):
            unknown = sorted(set(v) - set(FIELDS))
            missing = sorted(set(FIELDS) - set(v))
            if unknown:
                raise ValueError(f"unknown expected field(s): {', '.join(unknown)}")
            if missing:
                raise ValueError(f"missing expected field(s): {', '.join(missing)}")
        return v


class LabelSet(BaseModel):
    status: str
    version: int | str | None = None
    notes: str | None = None
    items: list[LabelItem]
    path: str | None = None  # where it was loaded from (set by load_labels, not part of the file)


def _item_error(ident: str, exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ())) or "(item)"
        parts.append(f"{loc}: {err.get('msg', '')}")
    return f"label item {ident}: " + "; ".join(parts)


def load_labels(path: str | Path) -> LabelSet:
    """Read and validate a label file; LabelFileError (naming the item) on anything malformed."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LabelFileError(f"label file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise LabelFileError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise LabelFileError(f"{path}: expected a mapping with status and items at the top level")
    status = data.get("status")
    if not isinstance(status, str) or not status.strip():
        raise LabelFileError(f"{path}: top-level 'status' must be a non-empty string")
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise LabelFileError(f"{path}: 'items' must be a non-empty list")
    unknown_top = sorted(set(data) - {"status", "version", "notes", "items"})
    if unknown_top:
        raise LabelFileError(f"{path}: unknown top-level key(s): {', '.join(unknown_top)}")

    items: list[LabelItem] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_items, start=1):
        ident = f"#{index}"
        if isinstance(raw, dict) and raw.get("id"):
            ident = f"{raw['id']} (#{index})"
        if not isinstance(raw, dict):
            raise LabelFileError(f"label item {ident}: expected a mapping, got {type(raw).__name__}")
        try:
            item = LabelItem.model_validate(raw)
        except ValidationError as exc:
            raise LabelFileError(_item_error(ident, exc)) from exc
        if item.id in seen:
            raise LabelFileError(f"label item {ident}: duplicate id")
        seen.add(item.id)
        items.append(item)
    try:
        return LabelSet(
            status=status.strip(),
            version=data.get("version"),
            notes=data.get("notes"),
            items=items,
            path=str(path),
        )
    except ValidationError as exc:
        raise LabelFileError(f"{path}: {describe_error(exc)}") from exc


def is_unverified(labelset: LabelSet) -> bool:
    """True when the file's status says UNVERIFIED (case-insensitive): the numbers are provisional."""
    return "unverified" in labelset.status.lower()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def jaccard(a: list[str] | set[str], b: list[str] | set[str]) -> float:
    """|A∩B| / |A∪B|; two empty lists agree completely (1.0)."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def field_correct(name: str, expected: PostingExtraction, got: PostingExtraction) -> bool:
    exp, act = getattr(expected, name), getattr(got, name)
    if name in LIST_FIELDS:
        return set(exp) == set(act)
    return exp == act


@dataclass(frozen=True)
class ItemResult:
    id: str
    status: str  # extraction status: "ok" | "failed"
    error: str | None = None
    got: dict[str, Any] | None = None  # the normalized extraction when ok
    wrong: tuple[str, ...] = ()  # fields that did not match (every field when failed)


@dataclass(frozen=True)
class FieldScore:
    name: str
    correct: int
    n: int
    mean_jaccard: float | None = None  # list fields only

    @property
    def accuracy(self) -> float:
        return self.correct / self.n if self.n else 0.0


@dataclass(frozen=True)
class EvalReport:
    model: str | None
    items: list[ItemResult]
    fields: list[FieldScore]
    started_at: datetime
    finished_at: datetime
    label_status: str = ""

    @property
    def n(self) -> int:
        return len(self.items)

    @property
    def failures(self) -> list[ItemResult]:
        return [r for r in self.items if r.status != "ok"]

    def field(self, name: str) -> FieldScore:
        return next(f for f in self.fields if f.name == name)


def score_item(item: LabelItem, outcome: ExtractionOutcome) -> ItemResult:
    if outcome.status != "ok" or outcome.extraction is None:
        return ItemResult(item.id, "failed", error=outcome.error or outcome.status, wrong=FIELDS)
    try:
        got = PostingExtraction.model_validate(outcome.extraction)
    except ValidationError as exc:  # ok outcomes are already validated; this guards a contract change
        return ItemResult(item.id, "failed", error=describe_error(exc), wrong=FIELDS)
    wrong = tuple(name for name in FIELDS if not field_correct(name, item.expected, got))
    return ItemResult(item.id, "ok", got=got.model_dump(mode="json"), wrong=wrong)


def score(items: list[LabelItem], results: list[ItemResult]) -> list[FieldScore]:
    """Per-field accuracy over all items (failures count as wrong) and mean Jaccard for list fields."""
    scores = []
    n = len(items)
    for name in FIELDS:
        correct = sum(1 for r in results if name not in r.wrong)
        mean_j = None
        if name in LIST_FIELDS:
            total = 0.0
            for item, r in zip(items, results, strict=True):
                if r.got is not None:
                    total += jaccard(getattr(item.expected, name), r.got[name])
            mean_j = total / n if n else 0.0
        scores.append(FieldScore(name, correct, n, mean_j))
    return scores


async def run_eval(
    router: ModelRouter,
    labelset: LabelSet,
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    now: datetime | None = None,
) -> EvalReport:
    """Extract every labeled item with the `extract` task and score it against its label.

    The keyword prefilter is bypassed: eval measures the model on every item. Raises before any call
    if the task cannot be resolved (no API key), and EvalAbortedError after
    MAX_CONSECUTIVE_PROVIDER_ERRORS provider errors in a row, so an outage never becomes a saved score.
    Bad replies (invalid JSON or schema, after extract_posting's one retry) are scored as failures.
    """
    _, model, _ = router.resolve(TASK)
    started = now or datetime.now().replace(microsecond=0)
    gate = asyncio.Semaphore(max(1, concurrency))
    state = {"consecutive": 0, "aborted": False}

    async def one(item: LabelItem) -> ExtractionOutcome | None:
        async with gate:
            if state["aborted"]:
                return None
            outcome = await extract_posting(router, item, company=item.company)
            if outcome.provider_error:
                state["consecutive"] += 1
                if state["consecutive"] >= MAX_CONSECUTIVE_PROVIDER_ERRORS:
                    state["aborted"] = True
            else:
                state["consecutive"] = 0
            return outcome

    outcomes = await asyncio.gather(*(one(item) for item in labelset.items))
    if state["aborted"]:
        raise EvalAbortedError(
            f"stopped after {MAX_CONSECUTIVE_PROVIDER_ERRORS} consecutive provider errors; "
            "no score recorded (check the API key, network and provider status)"
        )
    results = [score_item(item, o) for item, o in zip(labelset.items, outcomes, strict=True) if o is not None]
    finished = now or datetime.now().replace(microsecond=0)
    return EvalReport(
        model=model,
        items=results,
        fields=score(labelset.items, results),
        started_at=started,
        finished_at=finished,
        label_status=labelset.status,
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _banner_lines() -> list[str]:
    rule = "=" * len(UNVERIFIED_BANNER)
    return [rule, UNVERIFIED_BANNER, rule]


def format_report(report: EvalReport, unverified: bool) -> str:
    """Plain-text table (field, accuracy %, mean Jaccard for list fields, n), then failures and misses."""
    lines: list[str] = []
    if unverified:
        lines += [*_banner_lines(), ""]
    lines.append(f"Extraction eval: {report.n} items, model {report.model or 'unknown'}")
    lines.append("")
    lines.append(f"{'field':<16} {'accuracy':>9} {'jaccard':>8} {'n':>4}")
    lines.append("-" * 40)
    for f in report.fields:
        jac = f"{f.mean_jaccard:.2f}" if f.mean_jaccard is not None else "-"
        lines.append(f"{f.name:<16} {f.accuracy * 100:>8.1f}% {jac:>8} {f.n:>4}")
    failures = report.failures
    lines.append("")
    lines.append(f"Failed extractions: {len(failures)}")
    for r in failures:
        lines.append(f"  {r.id}: {r.error}")
    misses = [(name, [r.id for r in report.items if r.status == "ok" and name in r.wrong]) for name in FIELDS]
    misses = [(name, ids) for name, ids in misses if ids]
    if misses:
        lines.append("")
        lines.append("Wrong (excluding failures):")
        for name, ids in misses:
            lines.append(f"  {name}: {', '.join(ids)}")
    if unverified:
        lines += ["", UNVERIFIED_BANNER]
    return "\n".join(lines)


def summary(report: EvalReport, label_path: str | Path, unverified: bool) -> dict[str, Any]:
    """The compact JSON stored in kv `radar:last_eval` (read by `orbit radar stats`)."""
    return {
        "at": report.finished_at.isoformat(timespec="seconds"),
        "model": report.model,
        "label_file": str(label_path),
        "unverified": bool(unverified),
        "n": report.n,
        "failures": len(report.failures),
        "fields": {
            f.name: {
                "accuracy": round(f.accuracy, 4),
                "n": f.n,
                **({"mean_jaccard": round(f.mean_jaccard, 4)} if f.mean_jaccard is not None else {}),
            }
            for f in report.fields
        },
    }


async def save_last_eval(db: Database, report: EvalReport, label_path: str | Path, unverified: bool) -> dict[str, Any]:
    """Store the summary under kv `radar:last_eval` (overwriting the previous run) and return it."""
    value = summary(report, label_path, unverified)
    await db.kv_set(KV_LAST_EVAL, value)
    return value


def dry_run(labelset: LabelSet) -> str:
    """What `--dry-run` prints: counts only, no LLM calls."""
    items = labelset.items
    interns = sum(1 for i in items if i.expected.is_internship)
    by_source = Counter(i.source for i in items)
    lines: list[str] = []
    if is_unverified(labelset):
        lines += [*_banner_lines(), ""]
    lines.append(f"Label file: {labelset.path or '(in memory)'}")
    lines.append(f"Status: {labelset.status}")
    lines.append(f"Items: {len(items)} ({interns} internship, {len(items) - interns} non-internship)")
    lines.append("By source: " + ", ".join(f"{s} {by_source.get(s, 0)}" for s in SOURCES))
    noted = sum(1 for i in items if i.label_notes)
    lines.append(f"Items with label notes: {noted}")
    lines.append("Dry run: no extraction calls made.")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_LABELS_PATH",
    "KV_LAST_EVAL",
    "UNVERIFIED_BANNER",
    "EvalAbortedError",
    "EvalReport",
    "FieldScore",
    "ItemResult",
    "LabelFileError",
    "LabelItem",
    "LabelSet",
    "dry_run",
    "format_report",
    "is_unverified",
    "jaccard",
    "load_labels",
    "run_eval",
    "save_last_eval",
    "summary",
]
