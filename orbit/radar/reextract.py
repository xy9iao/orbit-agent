"""Re-extract already extracted postings through a message batch (`orbit radar reextract`).

Used after the `extract` route moves to a different model: every open posting with
extraction_status "ok" (it passed the prefilter and was extracted before) is sent again in one
Anthropic Message Batch (half price, asynchronous). The batch id and each posting's content_hash are
kept in kv (`radar:reextract`), so an interrupted run resumes collecting the same batch instead of
paying for a second one.

Collecting writes each valid result with the same content_hash guard as normal extraction (a posting
that changed meanwhile is left alone; the next poll re-extracts it). An invalid or errored result keeps
the old extraction: there is no retry round in a batch. When a field that goes into the embedding text
changes, `embedded_hash` is cleared so the next embed run re-embeds the posting, and then scoring runs
as usual. Nothing here notifies: notifications stay at most once per posting (UNIQUE posting_id).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update

from orbit.core.providers import Response
from orbit.core.router import ModelRouter
from orbit.radar.extract import (
    TASK,
    ExtractionOutcome,
    PendingPosting,
    build_messages,
    describe_error,
    parse_extraction,
    response_format,
    save_outcome,
)
from orbit.storage.db import Database
from orbit.storage.models import Company, Posting

KV_KEY = "radar:reextract"
EMBEDDED_FIELDS = ("terms", "locations", "role_type", "tech_stack")  # see match.embedding_text


def custom_id(posting_id: int) -> str:
    return f"posting-{posting_id}"


def _posting_id(cid: str) -> int | None:
    prefix = "posting-"
    return int(cid[len(prefix) :]) if cid.startswith(prefix) and cid[len(prefix) :].isdigit() else None


async def select_extracted(db: Database, *, limit: int | None = None) -> list[PendingPosting]:
    """Open postings whose extraction is ok, newest first."""
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
        .where(Posting.status == "open", Posting.extraction_status == "ok")
        .order_by(Posting.first_seen_at.desc(), Posting.id.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    async with db.session() as s:
        rows = (await s.execute(stmt)).all()
    return [PendingPosting(*row) for row in rows]


async def submit(db: Database, router: ModelRouter, postings: list[PendingPosting]) -> dict[str, Any]:
    """Send `postings` as one batch on the extract route and remember it in kv; returns that kv state."""
    requests = [(custom_id(p.id), build_messages(p, company=p.company)) for p in postings]
    batch_id = await router.submit_batch(TASK, requests, response_format=response_format())
    state = {
        "batch_id": batch_id,
        "model": router.resolve(TASK)[1],
        "submitted_at": datetime.now().replace(microsecond=0).isoformat(),
        "postings": {str(p.id): p.content_hash for p in postings},
    }
    await db.kv_set(KV_KEY, state)
    return state


@dataclass
class ReextractSummary:
    ok: int = 0
    failed: int = 0  # errored request or invalid JSON: the old extraction is kept
    stale: int = 0  # posting changed or disappeared since submission: nothing written
    changed_role_type: int = 0
    changed_locations: int = 0
    changed_internship: int = 0
    reembed: int = 0  # embedding text changed: embedded_hash cleared
    usage: dict[str, int] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)  # a few "title: old -> new" lines for the report


def _add_usage(total: dict[str, int], resp: Response) -> None:
    for key in ("prompt_tokens", "completion_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        total[key] = total.get(key, 0) + int(resp.usage.get(key, 0))


def _norm_locations(value: Any) -> list[str]:
    return sorted(str(v).strip().lower() for v in value) if isinstance(value, list) else []


async def collect(
    db: Database, router: ModelRouter, state: dict[str, Any], *, max_examples: int = 10
) -> ReextractSummary:
    """Write the finished batch's results to the postings it was submitted for, then clear the kv state."""
    results = await router.batch_results(TASK, state["batch_id"])
    hashes: dict[str, str] = state["postings"]
    async with db.session() as s:
        rows = (
            await s.execute(
                select(Posting.id, Posting.title, Posting.extraction, Posting.content_hash).where(
                    Posting.id.in_([int(i) for i in hashes])
                )
            )
        ).all()
    current = {row.id: row for row in rows}

    summary = ReextractSummary()
    for cid, result in results.items():
        pid = _posting_id(cid)
        if pid is None or str(pid) not in hashes:
            continue
        if isinstance(result, Response):
            _add_usage(summary.usage, result)
        row = current.get(pid)
        if row is None or row.content_hash != hashes[str(pid)]:
            summary.stale += 1
            continue
        if not isinstance(result, Response):
            summary.failed += 1
            continue
        try:
            new = parse_extraction(result.text).model_dump(mode="json")
        except (json.JSONDecodeError, ValidationError) as exc:
            summary.failed += 1
            error = describe_error(exc)
            if len(summary.examples) < max_examples:
                summary.examples.append(f"{row.title}: invalid result kept old extraction ({error})")
            continue

        old = row.extraction if isinstance(row.extraction, dict) else {}
        posting = PendingPosting(pid, "", row.title, "", None, "", hashes[str(pid)])
        if not await save_outcome(db, posting, ExtractionOutcome("ok", extraction=new, model=state.get("model"))):
            summary.stale += 1
            continue
        summary.ok += 1
        role_changed = old.get("role_type") != new.get("role_type")
        loc_changed = _norm_locations(old.get("locations")) != _norm_locations(new.get("locations"))
        summary.changed_role_type += role_changed
        summary.changed_locations += loc_changed
        summary.changed_internship += bool(old.get("is_internship")) != bool(new.get("is_internship"))
        if role_changed and len(summary.examples) < max_examples:
            summary.examples.append(f"{row.title}: role_type {old.get('role_type')} -> {new.get('role_type')}")
        if any(old.get(k) != new.get(k) for k in EMBEDDED_FIELDS):
            async with db.session() as s:
                await s.execute(
                    update(Posting)
                    .where(Posting.id == pid, Posting.content_hash == hashes[str(pid)])
                    .values(embedded_hash=None)
                )
                await s.commit()
            summary.reembed += 1

    await db.kv_set(KV_KEY, None)
    return summary


def describe(summary: ReextractSummary) -> str:
    u = summary.usage
    return (
        f"{summary.ok} re-extracted, {summary.failed} failed (old extraction kept), {summary.stale} stale; "
        f"changed: role_type {summary.changed_role_type}, locations {summary.changed_locations}, "
        f"is_internship {summary.changed_internship}; {summary.reembed} to re-embed; tokens: "
        f"input {u.get('prompt_tokens', 0)} (cache write {u.get('cache_creation_input_tokens', 0)}, "
        f"cache read {u.get('cache_read_input_tokens', 0)}), output {u.get('completion_tokens', 0)}"
    )
