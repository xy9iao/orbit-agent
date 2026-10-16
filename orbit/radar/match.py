"""Matching postings against the resume: posting embeddings (M5.3) and scoring (M5.4).

Which postings get an embedding: open postings whose `embedded_hash IS DISTINCT FROM content_hash`
(never embedded, or the content changed since), whose extraction is done (`ok` or `skipped`) and says
`is_internship = true`. In practice that is `ok` extractions of internships: prefilter-skipped rows
always store `is_internship: false`.
- Waiting for extraction means the embedding text includes the extracted fields and is not redone
  right after extraction lands. A posting whose extraction is pending or failed is embedded once it
  succeeds.
- Non-internships are never scored, notified or returned by the radar's rule filters, so embedding
  them would only add rows to keep fresh (6 KB of vector each). Embedding cost itself is tiny
  (text-embedding-3-small, about 1.5k tokens per posting).

Embedding text format (`embedding_text`), one field per line, empty fields omitted:

    Title: <title>
    Company: <company name>
    Terms: Summer 2027, Fall 2027            (extraction ok only)
    Locations: Toronto, ON; Remote (Canada)  (extraction ok only)
    Role type: ml_ai                         (extraction ok only)
    Tech stack: python, pytorch              (extraction ok only)
    <blank line>
    <description, cut near 6,000 chars at a paragraph/line/word break by the same helper extraction uses>

Scoring (`score_postings`, M5.4). Rule filters run in SQL (`eligible_postings_filter`):
- open, `extraction_status = 'ok'` and `is_internship = true`;
- term: an extracted term is in `radar.terms`, or the posting states no term at all
  (TERMS_NOT_STATED_PASSES; many internship postings never name the term);
- location: an extracted location is in `radar.locations`, or `remote_ok` and `canada_eligible` are both
  true. A posting with no extracted location passes only through the remote branch
  (LOCATIONS_NOT_STATED_PASSES is False): an unknown location is not evidence of a Canadian office.
- An empty `radar.terms` / `radar.locations` list in config.yaml switches that filter off (no preference).
Config values are normalized with the same helpers as extraction ("summer '27" -> "Summer 2027",
"Toronto" -> "Toronto, ON"), so they compare equal to the stored canonical forms.

Score = cosine similarity `1 - (posting.embedding <=> resume.embedding)`, computed by pgvector; with a goal
profile it is `0.8 * resume_sim + 0.2 * goal_sim`. Only postings whose embedding is current
(`embedded_hash = content_hash`) are scored. Every run rescores *all* eligible postings (a few hundred
rows, one UPDATE), so a new resume or goal takes effect on the next run without any bookkeeping. Open
postings that are scored but no longer eligible (or whose embedding went stale) get `score = NULL`, so a
stale score can never trigger a notification. Closed postings keep their last score (history only;
notifications and `top` read open postings).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from loguru import logger
from sqlalchemy import ColumnElement, Text, and_, cast, func, literal, or_, select, true, update
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from orbit.config import RadarConfig
from orbit.core.router import ModelRouter
from orbit.radar.extract import describe_error, truncate_description
from orbit.radar.models import normalize_location, normalize_terms, to_naive_local
from orbit.storage.db import Database
from orbit.storage.models import EMBEDDING_DIM, Company, Notification, Posting, Profile

EMBED_TASK = "embed"
# ~6k chars is ~1.5k tokens: the opening of a posting (role, team, requirements) carries the signal;
# benefits and legal boilerplate further down only dilute the vector.
MAX_EMBED_DESCRIPTION_CHARS = 6_000
# OpenAI accepts up to 2048 inputs per call; 64 keeps each request small and a failed batch cheap.
MAX_BATCH_SIZE = 64
# Extraction statuses whose postings may be embedded (see the module docstring).
EMBED_EXTRACTION_STATUSES = ("ok", "skipped")
# Consecutive failed batches after which embed_pending stops (the rest stay pending for the next run).
MAX_CONSECUTIVE_BATCH_ERRORS = 3

# Scoring rule decisions (see the module docstring and docs/DECISIONS.md).
TERMS_NOT_STATED_PASSES = True  # a posting that names no term passes the term filter
LOCATIONS_NOT_STATED_PASSES = False  # ...but one with no location must qualify as remote + Canada-eligible
RESUME_WEIGHT = 0.8  # score blend when a goal profile exists
GOAL_WEIGHT = 0.2


class EmbeddablePosting(Protocol):
    title: str
    description_text: str
    extraction_status: str
    extraction: dict[str, Any] | None


def _join(values: Any, sep: str) -> str:
    if not isinstance(values, list):
        return ""
    return sep.join(str(v) for v in values if str(v).strip())


def embedding_text(posting: EmbeddablePosting, company_name: str) -> str:
    """The text embedded for a posting; the format is documented in the module docstring."""
    lines = [f"Title: {(posting.title or '').strip()}", f"Company: {(company_name or '').strip()}"]
    extraction = posting.extraction if posting.extraction_status == "ok" else None
    if isinstance(extraction, dict):
        fields = (
            ("Terms", _join(extraction.get("terms"), ", ")),
            ("Locations", _join(extraction.get("locations"), "; ")),
            ("Role type", str(extraction.get("role_type") or "")),
            ("Tech stack", _join(extraction.get("tech_stack"), ", ")),
        )
        lines += [f"{label}: {value}" for label, value in fields if value]
    description = truncate_description(posting.description_text, MAX_EMBED_DESCRIPTION_CHARS)
    if description:
        lines += ["", description]
    return "\n".join(lines)


@dataclass(frozen=True)
class EmbedSummary:
    embedded: int = 0
    failed: int = 0  # postings in batches whose embed call failed; they stay pending
    batches: int = 0  # embed calls made
    stale: int = 0  # content changed during the call: vector discarded, the row stays pending
    aborted: bool = False  # stopped after MAX_CONSECUTIVE_BATCH_ERRORS failed batches in a row


@dataclass(frozen=True)
class PendingEmbedding:
    id: int
    content_hash: str
    text: str


async def select_pending_embeddings(db: Database, *, limit: int | None = None) -> list[PendingEmbedding]:
    """Open, extracted internship postings with a missing or stale embedding, newest first."""
    stmt = (
        select(
            Posting.id,
            Posting.content_hash,
            Posting.title,
            Posting.description_text,
            Posting.extraction_status,
            Posting.extraction,
            Company.name,
        )
        .join(Company, Company.id == Posting.company_id)
        .where(
            Posting.status == "open",
            Posting.embedded_hash.is_distinct_from(Posting.content_hash),
            Posting.extraction_status.in_(EMBED_EXTRACTION_STATUSES),
            Posting.extraction["is_internship"].astext == "true",
        )
        .order_by(Posting.first_seen_at.desc(), Posting.id.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    async with db.session() as s:
        rows = (await s.execute(stmt)).all()
    return [PendingEmbedding(row.id, row.content_hash, embedding_text(row, row.name)) for row in rows]


async def save_embeddings(db: Database, batch: Sequence[PendingEmbedding], vectors: Sequence[list[float]]) -> int:
    """Write each vector with `embedded_hash = content_hash`, only where the content_hash is unchanged.

    One short transaction per batch. Returns how many rows were written; the others changed while the
    embed call ran (a poll reset them) and keep their old `embedded_hash`, so the next run re-embeds them.
    """
    written = 0
    async with db.session() as s:
        for item, vector in zip(batch, vectors, strict=True):
            result = await s.execute(
                update(Posting)
                .where(Posting.id == item.id, Posting.content_hash == item.content_hash)
                .values(embedding=vector, embedded_hash=item.content_hash)
                .returning(Posting.id)
            )
            written += result.first() is not None
        await s.commit()
    return written


async def embed_pending(
    db: Database, router: ModelRouter, *, batch_size: int = MAX_BATCH_SIZE, limit: int | None = None
) -> EmbedSummary:
    """Embed up to `limit` pending postings in batches of at most `batch_size` (capped at 64).

    Rows are read in one short transaction; each batch's `router.embed` call runs with no DB session
    open, and its vectors are written in one short transaction with the content_hash guard. A failed
    batch (provider error, vector-count mismatch, wrong dimension) writes nothing, so its postings stay
    pending; MAX_CONSECUTIVE_BATCH_ERRORS failures in a row stop the run.

    Raises if the `embed` task cannot be resolved (no API key), before touching any row.
    """
    if limit is not None and limit <= 0:
        return EmbedSummary()
    router.resolve(EMBED_TASK)
    pending = await select_pending_embeddings(db, limit=limit)
    size = max(1, min(batch_size, MAX_BATCH_SIZE))

    embedded = failed = batches = stale = errors_in_a_row = 0
    aborted = False
    for start in range(0, len(pending), size):
        if errors_in_a_row >= MAX_CONSECUTIVE_BATCH_ERRORS:
            aborted = True
            break
        batch = pending[start : start + size]
        batches += 1
        try:
            vectors = await router.embed(EMBED_TASK, [item.text for item in batch])
            bad = [len(v) for v in vectors if len(v) != EMBEDDING_DIM]
            if bad:
                raise RuntimeError(
                    f"embedding has {bad[0]} dims, radar_postings.embedding is vector({EMBEDDING_DIM}); "
                    "the 'embed' task's model does not match the schema"
                )
        except Exception as e:  # noqa: BLE001 - recorded; the batch stays pending for the next run
            failed += len(batch)
            errors_in_a_row += 1
            logger.warning("embed: batch of {} failed: {}", len(batch), describe_error(e))
            continue
        errors_in_a_row = 0
        written = await save_embeddings(db, batch, vectors)
        embedded += written
        stale += len(batch) - written
    if aborted:
        logger.warning("embed: stopped after {} failed batches in a row", MAX_CONSECUTIVE_BATCH_ERRORS)
    return EmbedSummary(embedded=embedded, failed=failed, batches=batches, stale=stale, aborted=aborted)


def describe_embed(summary: EmbedSummary) -> str:
    text = (
        f"embedded {summary.embedded}, failed {summary.failed}, stale {summary.stale} "
        f"in {summary.batches} batches"
    )
    return text + (" (aborted after repeated errors)" if summary.aborted else "")


# ---- scoring (M5.4) ---------------------------------------------------------------------------------


def wanted_terms(terms: Iterable[str]) -> list[str]:
    """config.yaml terms in canonical "<Season> <YYYY>" form; an entry that can't be parsed is kept as written
    (it then matches nothing) with a warning, so one typo never disables scoring."""
    out: list[str] = []
    for term in terms:
        try:
            values = normalize_terms([term])
        except ValueError:
            logger.warning("radar.terms: cannot normalize {!r}; it will not match any posting", term)
            values = [term.strip()]
        out += [v for v in values if v not in out]
    return out


def wanted_locations(locations: Iterable[str]) -> list[str]:
    """config.yaml locations in canonical form ("Toronto" -> "Toronto, ON"); unparseable entries kept as written."""
    out: list[str] = []
    for location in locations:
        try:
            value = normalize_location(location)
        except ValueError:
            logger.warning("radar.locations: cannot normalize {!r}; it will not match any posting", location)
            value = location.strip()
        if value not in out:
            out.append(value)
    return out


def _text_array(values: Sequence[str]) -> ColumnElement[Any]:
    return cast(list(values), ARRAY(Text))


_EMPTY_JSON_ARRAY = cast(literal("[]"), JSONB)


def _flag(key: str) -> ColumnElement[bool]:
    return Posting.extraction[key].astext == "true"


def _not_stated(extracted: ColumnElement[Any]) -> ColumnElement[bool]:
    """True when a JSONB list field is missing, null or `[]`."""
    return func.coalesce(extracted, _EMPTY_JSON_ARRAY) == _EMPTY_JSON_ARRAY


def internship_filter() -> ColumnElement[bool]:
    """Open postings whose extraction succeeded and says it is an internship/co-op."""
    return and_(Posting.status == "open", Posting.extraction_status == "ok", _flag("is_internship"))


def term_filter(terms: Sequence[str]) -> ColumnElement[bool]:
    """An extracted term is one of `terms` (canonical form), or the posting states no term
    (TERMS_NOT_STATED_PASSES). Empty `terms` means no preference: always true."""
    if not terms:
        return true()
    extracted = Posting.extraction["terms"]
    overlap = extracted.has_any(_text_array(terms))
    if not TERMS_NOT_STATED_PASSES:
        return overlap
    return or_(overlap, _not_stated(extracted))


def location_filter(locations: Sequence[str]) -> ColumnElement[bool]:
    """An extracted location is one of `locations` (canonical form), or the posting is remote and open to
    Canada. Empty `locations` means no preference: always true."""
    if not locations:
        return true()
    extracted = Posting.extraction["locations"]
    branches = [
        extracted.has_any(_text_array(locations)),
        and_(_flag("remote_ok"), _flag("canada_eligible")),
    ]
    if LOCATIONS_NOT_STATED_PASSES:
        branches.append(_not_stated(extracted))
    return or_(*branches)


def eligible_postings_filter(config: RadarConfig) -> ColumnElement[bool]:
    """The radar's rule filters as one SQL condition: open internship with an ok extraction, wanted term,
    wanted location. Can be NULL for odd rows, so negate it with `.is_not(True)`, never `not_()`."""
    return and_(
        internship_filter(),
        term_filter(wanted_terms(config.terms)),
        location_filter(wanted_locations(config.locations)),
    )


def _similarity(profile_kind: str) -> ColumnElement[float]:
    """Cosine similarity between each posting and the stored `profile_kind` profile, computed by pgvector."""
    profile = select(Profile.embedding).where(Profile.kind == profile_kind).scalar_subquery()
    return 1 - Posting.embedding.cosine_distance(profile)


@dataclass(frozen=True)
class ScoreSummary:
    scored: int = 0  # eligible postings with a current embedding that got a score this run
    cleared: int = 0  # open postings whose old score was set to NULL (no longer eligible, or stale embedding)
    no_resume: bool = False  # nothing done: no resume profile embedded yet
    goal: bool = False  # the goal profile was blended in


async def _embedded_profiles(db: Database) -> set[str]:
    async with db.session() as s:
        rows = await s.execute(select(Profile.kind).where(Profile.embedding.is_not(None)))
        return {kind for (kind,) in rows}


async def score_postings(db: Database, config: RadarConfig, *, now: datetime | None = None) -> ScoreSummary:
    """Score every eligible posting against the resume (and goal) in SQL; clear scores of the others.

    Two UPDATEs in one transaction; see the module docstring for the rules. Without an embedded resume
    nothing is touched and `no_resume` is set.
    """
    profiles = await _embedded_profiles(db)
    if "resume" not in profiles:
        return ScoreSummary(no_resume=True)
    with_goal = "goal" in profiles
    stamp = to_naive_local(now) if now is not None else datetime.now().replace(microsecond=0)

    score = _similarity("resume")
    if with_goal:
        score = RESUME_WEIGHT * score + GOAL_WEIGHT * _similarity("goal")
    scorable = and_(
        eligible_postings_filter(config),
        Posting.embedding.is_not(None),
        Posting.embedded_hash == Posting.content_hash,
    )
    async with db.session() as s:
        scored = await s.execute(
            update(Posting).where(scorable).values(score=score, scored_at=stamp).returning(Posting.id)
        )
        n_scored = len(scored.all())
        cleared = await s.execute(
            update(Posting)
            .where(Posting.status == "open", Posting.score.is_not(None), scorable.is_not(True))
            .values(score=None, scored_at=None)
            .returning(Posting.id)
        )
        n_cleared = len(cleared.all())
        await s.commit()
    return ScoreSummary(scored=n_scored, cleared=n_cleared, goal=with_goal)


def describe_score(summary: ScoreSummary) -> str:
    if summary.no_resume:
        return "no resume embedded yet"
    return f"scored {summary.scored}, cleared {summary.cleared}" + (" (resume + goal)" if summary.goal else "")


# ---- best matches (M5.5) ----------------------------------------------------------------------------


@dataclass(frozen=True)
class TopMatch:
    score: float
    company: str
    title: str
    terms: list[str]
    locations: list[str]
    url: str
    notification: str | None  # radar_notifications.status (pending/sent/failed), None if never notified


@dataclass(frozen=True)
class ScoreCounts:
    scored: int  # open postings with a score
    above: int  # ...of which score >= threshold


async def top_matches(db: Database, limit: int, *, new_only: bool = False) -> list[TopMatch]:
    """The best open scored postings, best first; `new_only` drops postings that already have a notification."""
    stmt = (
        select(Posting.score, Company.name, Posting.title, Posting.extraction, Posting.url, Notification.status)
        .join(Company, Company.id == Posting.company_id)
        .outerjoin(Notification, Notification.posting_id == Posting.id)
        .where(Posting.status == "open", Posting.score.is_not(None))
        .order_by(Posting.score.desc(), Posting.first_seen_at.desc(), Posting.id.desc())
        .limit(limit)
    )
    if new_only:
        stmt = stmt.where(Notification.id.is_(None))
    async with db.session() as s:
        rows = (await s.execute(stmt)).all()
    out = []
    for score, company, title, extraction, url, notification in rows:
        fields = extraction if isinstance(extraction, dict) else {}
        terms = [str(t) for t in fields.get("terms") or []]
        locations = [str(loc) for loc in fields.get("locations") or []]
        out.append(TopMatch(float(score), company, title, terms, locations, url, notification))
    return out


async def score_counts(db: Database, threshold: float) -> ScoreCounts:
    stmt = select(
        func.count(Posting.id),
        func.count(Posting.id).filter(Posting.score >= threshold),
    ).where(Posting.status == "open", Posting.score.is_not(None))
    async with db.session() as s:
        scored, above = (await s.execute(stmt)).one()
    return ScoreCounts(int(scored), int(above))


async def why_nothing_scored(db: Database) -> str:
    """A one-line hint naming the first missing step when no open posting has a score."""
    eligible_extraction = internship_filter()
    stmt = select(
        func.count(Posting.id),
        func.count(Posting.id).filter(Posting.extraction_status == "pending"),
        func.count(Posting.id).filter(eligible_extraction),
        func.count(Posting.id).filter(eligible_extraction, Posting.embedded_hash == Posting.content_hash),
    ).where(Posting.status == "open")
    async with db.session() as s:
        open_, pending, internships, embedded = (await s.execute(stmt)).one()
    profiles = await _embedded_profiles(db)
    if not open_:
        return "no open postings yet: run `orbit radar seed`, then `orbit radar poll` (or start the daemon)"
    if not internships:
        if pending:
            return (
                f"{pending} open postings are not extracted yet: run `orbit radar extract` "
                "(needs OPENAI_API_KEY)"
            )
        return f"none of the {open_} open postings was extracted as an internship"
    if "resume" not in profiles:
        return "no resume embedded yet: run `orbit radar resume` (needs OPENAI_API_KEY)"
    if not embedded:
        return (
            f"{internships} internship postings have no current embedding: run `orbit radar embed` "
            "(needs OPENAI_API_KEY)"
        )
    return (
        f"none of the {embedded} embedded internship postings passes the rule filters: check radar.terms "
        "and radar.locations in config.yaml"
    )
