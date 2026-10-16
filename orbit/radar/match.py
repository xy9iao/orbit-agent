"""Matching postings against the resume. M5.3: posting embeddings; M5.4 adds scoring here.

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
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from loguru import logger
from sqlalchemy import select, update

from orbit.core.router import ModelRouter
from orbit.radar.extract import describe_error, truncate_description
from orbit.storage.db import Database
from orbit.storage.models import EMBEDDING_DIM, Company, Posting

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
