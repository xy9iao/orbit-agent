"""LLM extraction of structured posting fields (M4.2).

Flow for one posting: a cheap keyword `prefilter` on the title skips obvious non-student roles
without an LLM call; everything else goes through `router.complete("extract", ...)` with a strict
JSON-schema `response_format` generated from `PostingExtraction`, and the reply is validated by the
same Pydantic model (its validators normalize terms, locations, role type and tech stack).

Retry rule: a reply that is not JSON or fails validation is retried exactly once, with the bad reply
and the error fed back to the model. Provider and network exceptions are not retried here: the posting
is marked `failed` with the error, and `extract_pending(..., retry_failed=True)` picks it up later.

Stored shapes (`radar_postings.extraction`, JSONB):
- status `ok`: `PostingExtraction.model_dump(mode="json")`.
- status `skipped`: `{"is_internship": false, "skipped_reason": "<why>"}` with `extraction_model = "prefilter"`.
  It is deliberately *not* a full PostingExtraction (we would have to invent terms, role type and so
  on); downstream filters only read `extraction->>'is_internship'`, and anything that wants the full
  model must check `extraction_status = 'ok'` first.
- status `failed`: `extraction = NULL`, the error in `extraction_error`.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from loguru import logger
from pydantic import ValidationError
from sqlalchemy import select, update

from orbit.core.router import ModelRouter
from orbit.radar.models import PostingExtraction
from orbit.storage.db import Database
from orbit.storage.models import Company, Posting

TASK = "extract"
PREFILTER_MODEL = "prefilter"  # extraction_model for postings skipped without an LLM call

# Description budget per prompt. ~12k chars is ~3k tokens: enough for every section that states terms,
# locations and the stack, while capping cost on boards that paste whole benefits handbooks.
MAX_DESCRIPTION_CHARS = 12_000
TRUNCATION_MARKER = "\n\n[... description truncated]"
MAX_ERROR_CHARS = 1000
# Postings extracted at once by extract_pending: a few in flight hides LLM latency without
# tripping rate limits on a small OpenAI tier.
DEFAULT_CONCURRENCY = 3
# Consecutive provider/network errors after which extract_pending stops starting new calls (the rest
# stay pending), so an API outage marks a handful of postings failed instead of the whole batch.
MAX_CONSECUTIVE_PROVIDER_ERRORS = 3


class PostingText(Protocol):
    title: str
    location_raw: str
    department: str | None
    description_text: str


# ---------------------------------------------------------------------------
# Prefilter
# ---------------------------------------------------------------------------

# Title words that clearly mark a non-student role. Matched as whole words, case-insensitively.
# Roman level suffixes (II-V) only: plain digits clash with years ("Summer 2027").
NON_STUDENT_TITLE_PATTERNS: tuple[str, ...] = (
    r"senior", r"sr", r"staff", r"principal", r"lead", r"leader", r"manager", r"director", r"head of",
    r"vp", r"vice president", r"chief", r"architect", r"distinguished", r"fellow", r"president",
    r"ii", r"iii", r"iv", r"v",
)  # fmt: skip
# Words that mean "maybe a student role": any of them in the title or the start of the description
# vetoes the skip, so the LLM decides. New-grad roles are not skipped either (they are the ambiguous
# cases eval tracks, and they are few), only clearly senior ones.
STUDENT_PATTERNS: tuple[str, ...] = (
    r"intern", r"interns", r"internship", r"internships", r"co-?op", r"co op", r"student", r"students",
    r"pey", r"apprentice", r"apprenticeship", r"trainee", r"placement", r"stage", r"stagiaire",
    r"new grad", r"graduate", r"university", r"campus", r"early career", r"summer", r"fall", r"winter",
)  # fmt: skip
# How much of the description the student veto looks at (the opening usually says "this internship...").
PREFILTER_DESCRIPTION_CHARS = 600

_NON_STUDENT_RE = re.compile(r"\b(" + "|".join(NON_STUDENT_TITLE_PATTERNS) + r")\b\.?", re.IGNORECASE)
_STUDENT_RE = re.compile(r"\b(" + "|".join(STUDENT_PATTERNS) + r")\b", re.IGNORECASE)
# PhD-only roles ("PhD", "Ph.D.", "Ph D", "PhDs") are student roles but not for an undergrad, so these
# rules ignore the student veto.
_PHD_RE = re.compile(r"\bph\.?\s?d\.?s?\b", re.IGNORECASE)
_PHD = r"ph\.?\s?d\b\.?"
# "a PhD or Master's", "PhD/MS": another degree is accepted, so the PhD is not required.
_NOT_OTHER_DEGREE = (
    r"(?!\s*(?:/|,|or\b|and\b)\s*(?:an?\s+)?(?:master|m\.?sc?\b|m\.?eng|mba|bachelor|b\.?sc?\b|undergrad))"
)
# Description phrases that make a PhD a requirement. The PhD must follow the verb directly, so "pursuing
# a Bachelor's, Master's or PhD" does not match.
PHD_REQUIRED_PATTERNS: tuple[str, ...] = (
    rf"\b(?:pursuing|enrolled\s+in|enrollment\s+in|enrolment\s+in|working\s+towards?|completing|"
    rf"in\s+the\s+process\s+of\s+obtaining)\s+(?:(?:a|an|your)\s+)?(?:current\s+)?{_PHD}{_NOT_OTHER_DEGREE}",
    rf"\b{_PHD}\s*(?:students?|candidates?)\s+only\b",
    rf"\bonly\s+(?:open\s+)?(?:to\s+)?{_PHD}\s*(?:students?|candidates?)",
    rf"\bmust\s+be\s+(?:an?\s+)?(?:current\s+)?{_PHD}\s*(?:students?|candidates?)",
    rf"\b{_PHD}\s+(?:degree\s+)?(?:is\s+)?required\b",
)
# A requirement line that says any of these, or sits under a heading that does, only prefers a PhD.
_PREFERRED_RE = re.compile(
    r"\b(?:prefer|preferred|preferably|preference|ideally|plus|bonus|nice\s+to\s+have|desirable|desired|"
    r"advantage|advantageous|asset)\b",
    re.IGNORECASE,
)
# Headings that start a requirements section again (end of a "Preferred qualifications" section).
_SECTION_RE = re.compile(
    r"\b(?:requirements?|required|qualifications|minimum|basic|must\s+have|what\s+you|you\s+have|you\s+will|"
    r"you'll|you\s+may|about\s+you|who\s+you\s+are|responsibilities|what\s+we|benefits|perks)\b",
    re.IGNORECASE,
)
_HEADING_MAX_CHARS = 80
_PHD_REQUIRED_RE = re.compile("|".join(PHD_REQUIRED_PATTERNS), re.IGNORECASE)


def phd_requirement(description: str) -> str | None:
    """The description phrase that requires a PhD, or None when a PhD is optional, preferred or unmentioned.

    Reads line by line. A match is ignored when its line names a preference ("PhD preferred", "ideally
    pursuing a PhD") or when it sits under a short heading that does ("Preferred qualifications:"), until
    the next requirements-style heading.
    """
    in_preferred = False
    for line in (description or "").splitlines():
        text = line.strip().lstrip("-*•· ").strip()
        if not text:
            continue
        if len(text) <= _HEADING_MAX_CHARS:
            if _PREFERRED_RE.search(text) and not _PHD_RE.search(text):
                in_preferred = True
                continue
            if _SECTION_RE.search(text) and not _PHD_RE.search(text):
                in_preferred = False
                continue
        match = _PHD_REQUIRED_RE.search(text)
        if match and not in_preferred and not _PREFERRED_RE.search(text):
            return match.group(0)
    return None


def skip_reason(posting: PostingText) -> str | None:
    """Why the posting is not worth an LLM call, or None when the LLM should decide.

    Skips any title that names a PhD, and any description that requires one (see `phd_requirement`).
    Otherwise conservative: skip only when the title has a seniority/management word and neither the
    title nor the opening of the description mentions anything student-like.
    """
    title = posting.title or ""
    if _PHD_RE.search(title):
        return f"title: {title.strip()[:200]} (PhD role)"
    required = phd_requirement(posting.description_text or "")
    if required is not None:
        return f"description requires a PhD ({required.strip()[:100]!r})"
    senior = _NON_STUDENT_RE.search(title)
    if senior is None:
        return None
    opening = (posting.description_text or "")[:PREFILTER_DESCRIPTION_CHARS]
    if _STUDENT_RE.search(title) or _STUDENT_RE.search(opening):
        return None
    return f"title: {title.strip()[:200]} (matched {senior.group(1)!r})"


def prefilter(posting: PostingText) -> bool:
    """True when the posting should be skipped (marked `skipped`, is_internship=false) without an LLM call."""
    return skip_reason(posting) is not None


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

# Extra guidance on top of each field's own description in PostingExtraction.
_FIELD_RULES: dict[str, str] = {
    "is_internship": (
        '"University graduate", junior and entry-level roles count as new-grad (false) unless the posting '
        "says they are for current students."
    ),
    "terms": (
        'These are Waterloo co-op seasons; "Spring" means Summer (May-Aug). A 4-month role is one term. '
        "Do not infer terms from the posting date. Use [] when unsure."
    ),
    "locations": "List every place the role can be done. Do not add locations the posting does not name.",
    "canada_eligible": "Read the work-authorization text as well as the locations.",
    "role_type": "Choose the single best fit from the listed values.",
    "tech_stack": (
        "Only concrete technologies: programming languages, frameworks, libraries, databases, cloud "
        'platforms and tools. Not skills or concepts such as "algorithms", "machine learning", '
        '"distributed systems", "agile" or "communication".'
    ),
}


def _system_prompt() -> str:
    lines = [
        "You extract structured fields from one job posting for a University of Waterloo co-op student "
        "searching for internships. Use only what the posting says; never guess. Reply with one JSON "
        "object that matches the schema and nothing else.",
        "",
        "Fields:",
    ]
    for name, field in PostingExtraction.model_fields.items():
        rule = _FIELD_RULES.get(name, "")
        lines.append(f"- {name}: {field.description} {rule}".rstrip())
    return "\n".join(lines)


SYSTEM_PROMPT = _system_prompt()


def truncate_description(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """Cut `text` to at most `limit` chars (plus a marker), preferring a paragraph, then line, then word break.

    A break point is only used if it keeps at least half the budget, so one giant paragraph is still
    cut near the limit rather than near the start.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    for sep in ("\n\n", "\n", " "):
        cut = head.rfind(sep)
        if cut >= limit // 2:
            head = head[:cut]
            break
    return head.rstrip() + TRUNCATION_MARKER


def build_messages(posting: PostingText, *, company: str | None = None) -> list[dict[str, str]]:
    """System prompt with the field definitions, plus the posting as the user message."""
    company = company if company is not None else getattr(posting, "company", "") or ""
    user = "\n".join(
        [
            f"Company: {company or 'unknown'}",
            f"Title: {posting.title}",
            f"Location (as listed): {posting.location_raw or 'not listed'}",
            f"Department: {posting.department or 'not listed'}",
            "",
            "Description:",
            truncate_description(posting.description_text) or "(no description)",
        ]
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# One posting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionOutcome:
    status: str  # "ok" | "failed" | "skipped"
    extraction: dict[str, Any] | None = None
    error: str | None = None
    model: str | None = None
    provider_error: bool = False  # failed because the provider/network raised (not a bad reply)


def response_format() -> dict[str, Any]:
    return {"type": "json_schema", "json_schema": PostingExtraction.openai_json_schema()}


def _clip(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def describe_error(exc: BaseException) -> str:
    """Concise "Type: detail" text (≤1000 chars); validation errors list field paths and messages."""
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err.get("loc", ())) or "(root)"
            parts.append(f"{loc}: {err.get('msg', '')}")
        detail = "; ".join(parts)
    elif isinstance(exc, json.JSONDecodeError):
        detail = f"{exc.msg} at line {exc.lineno} column {exc.colno}"
    else:
        detail = "\n".join(str(exc).strip().splitlines()[:3])
    return _clip(f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__)


def _strip_fences(text: str) -> str:
    """Tolerate a reply wrapped in ```json fences (strict mode should never do this, other models might)."""
    text = text.strip()
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return m.group(1) if m else text


def parse_extraction(text: str) -> PostingExtraction:
    """JSON text -> validated PostingExtraction. Raises JSONDecodeError or ValidationError."""
    return PostingExtraction.model_validate(json.loads(_strip_fences(text)))


def _model_name(router: ModelRouter) -> str | None:
    try:
        return router.resolve(TASK)[1]
    except Exception:  # noqa: BLE001 - only used to label the outcome
        return None


async def extract_posting(
    router: ModelRouter, posting: PostingText, *, company: str | None = None
) -> ExtractionOutcome:
    """Run the LLM on one posting; never raises for model or provider errors (returns status failed)."""
    model = _model_name(router)
    messages = build_messages(posting, company=company)
    error = ""
    for attempt in (1, 2):
        try:
            response = await router.complete(TASK, messages, response_format=response_format())
        except Exception as exc:  # noqa: BLE001 - provider/network: recorded, retried on a later run
            logger.warning("extract: provider error on '{}': {}", posting.title, describe_error(exc))
            return ExtractionOutcome("failed", error=describe_error(exc), model=model, provider_error=True)
        try:
            extraction = parse_extraction(response.text)
        except (json.JSONDecodeError, ValidationError) as exc:
            error = describe_error(exc)
            logger.info("extract: attempt {} rejected for '{}': {}", attempt, posting.title, error)
            messages = [
                *messages,
                {"role": "assistant", "content": response.text or ""},
                {
                    "role": "user",
                    "content": f"Your reply was rejected: {error}\n"
                    "Reply again with only a JSON object that matches the schema and the field rules.",
                },
            ]
            continue
        return ExtractionOutcome("ok", extraction=extraction.model_dump(mode="json"), model=model)
    return ExtractionOutcome("failed", error=_clip(f"after retry: {error}"), model=model)


def skipped_outcome(reason: str) -> ExtractionOutcome:
    return ExtractionOutcome(
        "skipped", extraction={"is_internship": False, "skipped_reason": reason}, model=PREFILTER_MODEL
    )


async def extract_or_skip(
    router: ModelRouter, posting: PostingText, *, company: str | None = None
) -> ExtractionOutcome:
    reason = skip_reason(posting)
    if reason is not None:
        return skipped_outcome(reason)
    return await extract_posting(router, posting, company=company)


# ---------------------------------------------------------------------------
# Batch over the database
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingPosting:
    """What extraction needs from a row, read in a short transaction and then used outside it."""

    id: int
    company: str
    title: str
    location_raw: str
    department: str | None
    description_text: str
    content_hash: str


@dataclass(frozen=True)
class ExtractSummary:
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    stale: int = 0  # content changed while the LLM ran: result discarded, the row stays pending
    aborted: bool = False  # stopped early after repeated provider errors; unstarted postings stay pending

    @property
    def processed(self) -> int:
        return self.ok + self.failed + self.skipped


async def select_pending(db: Database, *, limit: int | None, retry_failed: bool = False) -> list[PendingPosting]:
    """Open postings awaiting extraction (plus failed ones when retry_failed), newest first."""
    statuses = ["pending", "failed"] if retry_failed else ["pending"]
    stmt = (
        select(
            Posting.id,
            Company.name,
            Posting.title,
            Posting.location_raw,
            Posting.department,
            Posting.description_text,
            Posting.content_hash,
        )
        .join(Company, Company.id == Posting.company_id)
        .where(Posting.status == "open", Posting.extraction_status.in_(statuses))
        .order_by(Posting.first_seen_at.desc(), Posting.id.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    async with db.session() as s:
        rows = (await s.execute(stmt)).all()
    return [PendingPosting(*row) for row in rows]


async def save_outcome(
    db: Database, posting: PendingPosting, outcome: ExtractionOutcome, *, now: datetime | None = None
) -> bool:
    """Persist one outcome; False (nothing written) if the row's content_hash changed since it was read.

    The `content_hash` guard matters because the LLM call runs outside any transaction: a poll that
    updates the posting meanwhile resets it to pending with a new hash, and this stale result must not
    overwrite that reset. The next run extracts the new content.
    """
    now = now or datetime.now().replace(microsecond=0)
    stmt = (
        update(Posting)
        .where(Posting.id == posting.id, Posting.content_hash == posting.content_hash)
        .values(
            extraction=outcome.extraction,
            extraction_status=outcome.status,
            extraction_error=outcome.error,
            extraction_model=outcome.model,
            extracted_at=now,
        )
        .returning(Posting.id)
    )
    async with db.session() as s:
        written = (await s.execute(stmt)).first() is not None
        await s.commit()
    return written


async def extract_pending(
    db: Database,
    router: ModelRouter,
    *,
    limit: int | None,
    retry_failed: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    now: datetime | None = None,
) -> ExtractSummary:
    """Extract up to `limit` pending open postings (newest first) and persist each result.

    No DB connection is held during LLM calls: rows are read in one short transaction, each posting is
    extracted (or prefiltered) outside it, and each result is written in its own short transaction with
    the content_hash guard. Up to `concurrency` postings are in flight at once. A failure on one posting
    never stops the others, except that MAX_CONSECUTIVE_PROVIDER_ERRORS provider errors in a row stop
    new calls (the remaining postings stay pending for the next run).

    Raises if the `extract` task cannot be resolved (for example no API key for its provider), before
    touching any posting, so a configuration problem never marks postings failed.
    """
    if limit is not None and limit <= 0:
        return ExtractSummary()
    router.resolve(TASK)
    postings = await select_pending(db, limit=limit, retry_failed=retry_failed)
    if not postings:
        return ExtractSummary()

    counts = {"ok": 0, "failed": 0, "skipped": 0, "stale": 0}
    state = {"consecutive_provider_errors": 0, "aborted": False}
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(posting: PendingPosting) -> None:
        async with gate:
            if state["aborted"]:
                return
            reason = skip_reason(posting)
            if reason is not None:
                outcome = skipped_outcome(reason)
            else:
                outcome = await extract_posting(router, posting, company=posting.company)
                if outcome.provider_error:
                    state["consecutive_provider_errors"] += 1
                    if state["consecutive_provider_errors"] >= MAX_CONSECUTIVE_PROVIDER_ERRORS:
                        state["aborted"] = True
                else:
                    state["consecutive_provider_errors"] = 0
            if await save_outcome(db, posting, outcome, now=now):
                counts[outcome.status] += 1
            else:
                counts["stale"] += 1
                logger.info("extract: posting {} changed during extraction; result discarded", posting.id)

    await asyncio.gather(*(one(p) for p in postings))
    if state["aborted"]:
        logger.warning("extract: stopped after {} consecutive provider errors", MAX_CONSECUTIVE_PROVIDER_ERRORS)
    return ExtractSummary(**counts, aborted=bool(state["aborted"]))
