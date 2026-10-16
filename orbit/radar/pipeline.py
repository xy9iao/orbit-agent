"""One radar run: poll every board, extract pending postings, embed and score them, then claim new
matches for notification (M4.3, M5.3, M5.4, M6.1).

`run_pipeline` is what the watcher calls every interval. Stages run in order and are isolated: each one
is wrapped so an exception (DB hiccup, provider outage, bug) is logged and recorded in the summary,
and the next stage still runs. Nothing raises to the scheduler.

Stages:
1. poll: `poll_all` over the active companies (needs no LLM key).
2. extract: `extract_pending(limit=radar.max_extract_per_poll)`, newest first, then a small
   `retry_failed_extractions` pass (see below). Skipped with one warning when no provider serves the
   `extract` task (no API key), so a fresh install without OPENAI_API_KEY still polls.
3. embed: `match.embed_pending` over every extracted internship posting with a missing or stale
   embedding (no cap: embeddings are cheap, and the extract cap already bounds how many become
   eligible per run). Skipped with one warning when no provider serves the `embed` task.
4. score: `match.score_postings` rescores every eligible posting against the resume (and goal) in SQL
   and clears the scores of open postings that no longer pass the rule filters. Needs no API key, so it
   runs even when embed was skipped (it then just refreshes existing scores). Without an embedded
   resume it does nothing and logs one warning per run.

5. notify (M6.1), only when the caller passes `notify=True` (the watcher, which pushes the events):
   `notify.notify_matches` claims open postings above `radar.notify_threshold` that were never notified
   (plus due retries) and renders them as `Event`s, exposed as `summary.events`. Needs no API key. Off
   by default because a claimed posting that nobody pushes is never offered again (at-most-once, see
   notify.py), so only a caller that will push the events may claim them.

Automatic retry of failed extractions: a posting that failed on a provider/network error stays
`failed` until something retries it. Each run retries at most RETRY_FAILED_PER_POLL such postings
whose last attempt is older than RETRY_FAILED_AFTER, oldest attempt first. Postings that failed
because the model's reply was invalid twice (error starting with "after retry:") are *not* retried
automatically, since the same prompt would most likely fail again; `orbit radar extract
--retry-failed` retries them by hand.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import func, or_, select

from orbit.config import RadarConfig
from orbit.core.router import ModelRouter
from orbit.radar import extract as ex, match, notify as nt
from orbit.radar.extract import ExtractSummary, PendingPosting
from orbit.radar.match import EmbedSummary, ScoreSummary, describe_embed, describe_score
from orbit.radar.models import to_naive_local
from orbit.radar.notify import NotifySummary, describe_notify
from orbit.radar.poll import PollSummary, describe_error, poll_all
from orbit.scheduler.watchers.base import Event
from orbit.storage.db import Database
from orbit.storage.models import Company, Posting

if TYPE_CHECKING:
    from orbit.radar.sources.http import PoliteClient

# Failed extractions retried automatically per run, and how long after the last attempt.
RETRY_FAILED_PER_POLL = 3
RETRY_FAILED_AFTER = timedelta(hours=6)
# extract.py prefixes failures where the model's reply was rejected twice (not a provider error) with this.
VALIDATION_FAILURE_PREFIX = "after retry:"


@dataclass
class PipelineSummary:
    poll: PollSummary | None = None
    extract: ExtractSummary | None = None
    retried: ExtractSummary | None = None  # automatic retry of old provider-error failures
    embed: EmbedSummary | None = None
    score: ScoreSummary | None = None
    notify: NotifySummary | None = None
    skipped: dict[str, str] = field(default_factory=dict)  # stage -> why it did not run
    errors: dict[str, str] = field(default_factory=dict)  # stage -> "Type: first line" of what it raised
    seconds: float = 0.0

    @property
    def events(self) -> list[Event]:
        """Notification events for the scheduler to push (empty unless the notify stage ran)."""
        return list(self.notify.events) if self.notify is not None else []

    @property
    def ok(self) -> bool:
        return not self.errors


def _stamp(now: datetime | None) -> datetime:
    return to_naive_local(now) if now is not None else datetime.now().replace(microsecond=0)


def missing_provider(router: ModelRouter | None, task: str) -> str | None:
    """None when `task` resolves to a configured provider, otherwise a one-line hint for the log."""
    if router is None:
        return f"no model router for task '{task}'"
    try:
        router.resolve(task)
        return None
    except KeyError:
        return f"config.yaml has no '{task}' task (and no 'chat' fallback)"
    except RuntimeError:
        tc = router.config.tasks.get(task) or router.config.tasks.get("chat")
        pc = router.config.providers.get(tc.provider) if tc is not None else None
        env = pc.api_key_env if pc is not None else "the provider's API key"
        return f"no provider for task '{task}' — set {env}"


async def retry_failed_extractions(
    db: Database,
    router: ModelRouter,
    *,
    limit: int = RETRY_FAILED_PER_POLL,
    older_than: timedelta = RETRY_FAILED_AFTER,
    now: datetime | None = None,
) -> ExtractSummary:
    """Re-extract up to `limit` open postings that failed on a provider error at least `older_than` ago.

    Runs one posting at a time and stops at the first provider error (the outage is probably still on).
    Results go through extract.py's `save_outcome`, so the content_hash guard applies.
    """
    if limit <= 0:
        return ExtractSummary()
    stamp = _stamp(now)
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
        .where(
            Posting.status == "open",
            Posting.extraction_status == "failed",
            or_(Posting.extracted_at.is_(None), Posting.extracted_at <= stamp - older_than),
            ~func.coalesce(Posting.extraction_error, "").startswith(VALIDATION_FAILURE_PREFIX),
        )
        .order_by(Posting.extracted_at.asc().nulls_first(), Posting.id)
        .limit(limit)
    )
    async with db.session() as s:
        postings = [PendingPosting(*row) for row in (await s.execute(stmt)).all()]

    counts = {"ok": 0, "failed": 0, "skipped": 0, "stale": 0}
    aborted = False
    for posting in postings:
        outcome = await ex.extract_or_skip(router, posting, company=posting.company)
        written = await ex.save_outcome(db, posting, outcome, now=stamp)
        counts[outcome.status if written else "stale"] += 1
        if outcome.provider_error:
            aborted = True
            break
    return ExtractSummary(**counts, aborted=aborted)


async def _stage_extract(db: Database, router: ModelRouter, config: RadarConfig, summary: PipelineSummary,
                         now: datetime | None) -> None:
    summary.extract = await ex.extract_pending(db, router, limit=config.max_extract_per_poll, now=now)
    if summary.extract.aborted:
        return  # the provider is failing; don't spend more calls on retries this run
    summary.retried = await retry_failed_extractions(db, router, now=now)


async def run_pipeline(
    db: Database,
    router: ModelRouter | None,
    client: PoliteClient,
    config: RadarConfig,
    *,
    now: datetime | None = None,
    notify: bool = False,
) -> PipelineSummary:
    """Poll, extract, embed, score, then notify if asked. Never raises (except on cancellation); see the
    module docstring."""
    t0 = time.perf_counter()
    summary = PipelineSummary()

    try:
        summary.poll = await poll_all(db, client, now=now)
    except Exception as e:  # noqa: BLE001 - a stage failure must not stop later stages
        summary.errors["poll"] = describe_error(e)
        logger.opt(exception=e).error("radar pipeline: poll stage failed: {}", summary.errors["poll"])

    hint = missing_provider(router, ex.TASK)
    if hint is not None:
        summary.skipped["extract"] = hint
        logger.warning("radar pipeline: extraction skipped: {}", hint)
    else:
        try:
            await _stage_extract(db, router, config, summary, now)
        except Exception as e:  # noqa: BLE001
            summary.errors["extract"] = describe_error(e)
            logger.opt(exception=e).error("radar pipeline: extract stage failed: {}", summary.errors["extract"])

    hint = missing_provider(router, match.EMBED_TASK)
    if hint is not None:
        summary.skipped["embed"] = hint
        logger.warning("radar pipeline: embedding skipped: {}", hint)
    else:
        try:
            summary.embed = await match.embed_pending(db, router)
        except Exception as e:  # noqa: BLE001
            summary.errors["embed"] = describe_error(e)
            logger.opt(exception=e).error("radar pipeline: embed stage failed: {}", summary.errors["embed"])

    try:
        summary.score = await match.score_postings(db, config, now=now)
        if summary.score.no_resume:
            logger.warning("radar pipeline: scoring skipped: no resume embedded yet — run `orbit radar resume`")
    except Exception as e:  # noqa: BLE001
        summary.errors["score"] = describe_error(e)
        logger.opt(exception=e).error("radar pipeline: score stage failed: {}", summary.errors["score"])

    if notify:
        try:
            summary.notify = await nt.notify_matches(db, config, now=now)
        except Exception as e:  # noqa: BLE001
            summary.errors["notify"] = describe_error(e)
            logger.opt(exception=e).error("radar pipeline: notify stage failed: {}", summary.errors["notify"])

    summary.seconds = time.perf_counter() - t0
    return summary


def describe_poll(summary: PollSummary) -> str:
    """Companies ok/failed and posting changes."""
    return (
        f"companies {summary.ok} ok / {summary.failed} failed, "
        f"postings {summary.new} new, {summary.updated} updated, "
        f"{summary.closed} closed, {summary.reopened} reopened"
    )


def describe_extract(summary: ExtractSummary) -> str:
    text = f"ok {summary.ok}, failed {summary.failed}, skipped {summary.skipped}, stale {summary.stale}"
    return text + (" (aborted after provider errors)" if summary.aborted else "")


def describe(summary: PipelineSummary) -> str:
    """One line for the log covering every stage."""
    parts = []
    if summary.poll is not None:
        parts.append(f"poll: {describe_poll(summary.poll)}")
    if summary.extract is not None:
        extract = f"extract: {describe_extract(summary.extract)}"
        if summary.retried is not None and summary.retried.processed + summary.retried.stale:
            extract += f"; retried failed: {describe_extract(summary.retried)}"
        parts.append(extract)
    if summary.embed is not None:
        parts.append(f"embed: {describe_embed(summary.embed)}")
    if summary.score is not None:
        parts.append(f"score: {describe_score(summary.score)}")
    if summary.notify is not None:
        parts.append(f"notify: {describe_notify(summary.notify)}")
    for stage, reason in summary.skipped.items():
        parts.append(f"{stage}: skipped ({reason})")
    for stage, error in summary.errors.items():
        parts.append(f"{stage}: ERROR {error}")
    return " | ".join(parts) + f" in {summary.seconds:.1f}s"
