"""Telegram notifications for new high-scoring postings (M6.1): claim, render, record the delivery result.

Flow of one radar run (the watcher's `check`, then the scheduler's `_poll_watcher`):
1. `claim_new_matches` (the pipeline's notify stage) claims what to send:
   - expire: `pending` rows older than STALE_PENDING_AFTER become `failed` with attempts = MAX_ATTEMPTS
     (see "Crash safety" below), so they are never offered again;
   - new: open postings with `score >= radar.notify_threshold` and no `radar_notifications` row are
     claimed with one `INSERT … SELECT … ON CONFLICT (posting_id) DO NOTHING RETURNING` (status `pending`,
     attempts 0). The UNIQUE posting_id makes the claim atomic: two concurrent runs can never both claim
     a posting, and a posting that already has a row (sent, failed, pending) is never claimed again,
     whatever happens to its score or status later;
   - retry: `failed` rows with attempts < MAX_ATTEMPTS whose posting is still open and still above the
     threshold go back to `pending` with a conditional `UPDATE … WHERE status = 'failed' RETURNING`
     (row-locked, so also claimed at most once per run).
   New and retried matches are merged and sorted best score first.
2. `build_events` renders them: one `Event` per match for the best `max_notifications_per_poll`, and
   the overflow in ONE digest event. Every event carries `data = {"posting_ids": [...], "kind": ...}`.
   Overflow postings are claimed too: the digest names up to DIGEST_MAX_LINES of them and counts the
   rest ("and N more — run `orbit radar top`"), and its push result decides all of their rows. Leaving
   them unclaimed would send another 5 + digest on every run until the backlog drained, which is the
   flood the cap exists to prevent.
3. `_poll_watcher` pushes each event through `OrbitApp.push` and calls the watcher's `on_pushed(event,
   ok)`, which calls `record_delivery`: `sent` (sent_at = now) on success, otherwise `failed` with
   attempts + 1 and an error. A failed row is retried on later runs, at most MAX_ATTEMPTS pushes in all.

Crash safety ("never twice" wins over "always once"): a row still `pending` at the start of a later run
was claimed by a run whose process stopped (or whose DB write failed) before `on_pushed` recorded the
result, so the message may or may not have reached Telegram. Re-sending could duplicate it, so a
`pending` row older than STALE_PENDING_AFTER is marked `failed` with attempts = MAX_ATTEMPTS and
EXPIRED_ERROR and is never offered again (at-most-once). A crash can lose one notification (the posting
still shows in `orbit radar top`), but it can never cause a duplicate. Delivery is at-least-once only
across explicit failures: a push that reported failure but in fact reached Telegram (a timeout after
the server accepted it) is retried, and can then arrive twice.

`max_notifications_per_poll = 0` turns notifications off: nothing is claimed, so turning them back on
later sends every match that is still open and above the threshold (capped and digested as usual).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orbit.config import RadarConfig
from orbit.radar.models import to_naive_local
from orbit.scheduler.watchers.base import Event
from orbit.storage.db import Database
from orbit.storage.models import Company, Notification, Posting

# Pushes per posting, first attempt included.
MAX_ATTEMPTS = 3
# A `pending` row this old was left behind by a run that never recorded its delivery result. A normal
# run records it within seconds; the watcher runs every 30 minutes and never overlaps itself.
STALE_PENDING_AFTER = timedelta(hours=1)
EXPIRED_ERROR = "delivery unknown (process stopped before confirmation)"
PUSH_FAILED_ERROR = "push failed: no channel delivered the message"
# Telegram rejects messages over 4096 chars; the scheduler prefixes "[internship_radar] ", so keep the
# rendered title + "\n" + body comfortably below that.
MAX_MESSAGE_CHARS = 3900
MAX_TITLE_CHARS = 200  # job titles, per line of a match message or a digest line
DIGEST_MAX_LINES = 15
ELLIPSIS = "…"

KIND_MATCH = "match"
KIND_DIGEST = "digest"


@dataclass(frozen=True)
class ClaimedMatch:
    posting_id: int
    score: float
    company: str
    title: str
    terms: list[str]
    locations: list[str]
    url: str
    attempts: int = 0  # earlier failed pushes (0 for a first claim)


@dataclass(frozen=True)
class NotifySummary:
    new: int = 0  # postings claimed for the first time
    retried: int = 0  # failed rows offered again
    expired: int = 0  # stale pending rows marked failed (never re-sent)
    matches: int = 0  # single-posting events built
    digested: int = 0  # postings carried by the digest event
    disabled: bool = False  # max_notifications_per_poll = 0
    events: list[Event] = field(default_factory=list)


def _stamp(now: datetime | None) -> datetime:
    return to_naive_local(now) if now is not None else datetime.now().replace(microsecond=0)


def _strings(values: Any) -> list[str]:
    return [str(v) for v in values if str(v).strip()] if isinstance(values, list) else []


def _above_threshold(config: RadarConfig) -> Any:
    return and_(Posting.status == "open", Posting.score.is_not(None), Posting.score >= config.notify_threshold)


async def expire_stale_pending(db: Database, *, now: datetime | None = None) -> int:
    """Mark `pending` rows older than STALE_PENDING_AFTER as failed for good (see "Crash safety")."""
    stamp = _stamp(now)
    async with db.session() as s:
        result = await s.execute(
            update(Notification)
            .where(Notification.status == "pending", Notification.created_at <= stamp - STALE_PENDING_AFTER)
            .values(status="failed", attempts=func.greatest(Notification.attempts, MAX_ATTEMPTS), error=EXPIRED_ERROR)
            .returning(Notification.id)
        )
        expired = len(result.all())
        await s.commit()
    return expired


async def _claim_new(db: Database, config: RadarConfig, stamp: datetime) -> dict[int, int]:
    """INSERT … SELECT … ON CONFLICT DO NOTHING RETURNING: {posting_id: attempts (0)} actually claimed."""
    has_row = select(Notification.id).where(Notification.posting_id == Posting.id).exists()
    candidates = select(
        Posting.id, Posting.score, literal("pending"), literal(0), literal(stamp)
    ).where(_above_threshold(config), ~has_row)
    stmt = (
        pg_insert(Notification)
        .from_select(
            [Notification.posting_id, Notification.score, Notification.status, Notification.attempts,
             Notification.created_at],
            candidates,
        )
        .on_conflict_do_nothing(index_elements=[Notification.posting_id])
        .returning(Notification.posting_id)
    )
    async with db.session() as s:
        claimed = {posting_id: 0 for (posting_id,) in (await s.execute(stmt)).all()}
        await s.commit()
    return claimed


async def _claim_retries(db: Database, config: RadarConfig) -> dict[int, int]:
    """failed -> pending for rows with attempts left whose posting is still open and above the threshold."""
    stmt = (
        update(Notification)
        .where(
            Notification.status == "failed",
            Notification.attempts < MAX_ATTEMPTS,
            Notification.posting_id == Posting.id,
            _above_threshold(config),
        )
        .values(status="pending", score=Posting.score, error=None)
        .returning(Notification.posting_id, Notification.attempts)
    )
    async with db.session() as s:
        claimed = {posting_id: attempts for posting_id, attempts in (await s.execute(stmt)).all()}
        await s.commit()
    return claimed


async def _details(db: Database, attempts: dict[int, int]) -> list[ClaimedMatch]:
    if not attempts:
        return []
    stmt = (
        select(Posting.id, Posting.score, Company.name, Posting.title, Posting.extraction, Posting.url)
        .join(Company, Company.id == Posting.company_id)
        .where(Posting.id.in_(list(attempts)))
        .order_by(Posting.score.desc(), Posting.first_seen_at.desc(), Posting.id.desc())
    )
    async with db.session() as s:
        rows = (await s.execute(stmt)).all()
    out = []
    for posting_id, score, company, title, extraction, url in rows:
        fields = extraction if isinstance(extraction, dict) else {}
        out.append(
            ClaimedMatch(
                posting_id=posting_id,
                score=float(score) if score is not None else 0.0,
                company=company,
                title=title,
                terms=_strings(fields.get("terms")),
                locations=_strings(fields.get("locations")),
                url=url,
                attempts=attempts[posting_id],
            )
        )
    return out


async def claim_new_matches(db: Database, config: RadarConfig, *, now: datetime | None = None) -> list[ClaimedMatch]:
    """Expire stale pending rows, then claim new matches and due retries; best score first.

    Every returned match has a `pending` row that this call (and no concurrent one) claimed; the caller
    must deliver it and then call `record_delivery`. See the module docstring for the rules.
    """
    matches, _, _ = await _claim(db, config, now=now)
    return matches


async def _claim(db: Database, config: RadarConfig, *, now: datetime | None) -> tuple[list[ClaimedMatch], int, int]:
    stamp = _stamp(now)
    expired = await expire_stale_pending(db, now=stamp)
    retries = await _claim_retries(db, config)
    new = await _claim_new(db, config, stamp)
    return await _details(db, {**new, **retries}), len(new), expired


# ---- rendering --------------------------------------------------------------------------------------


def _cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - len(ELLIPSIS)].rstrip() + ELLIPSIS


def _fit(title: str, body: str) -> tuple[str, str]:
    """Keep `title + "\\n" + body` within MAX_MESSAGE_CHARS, cutting the body's end first."""
    title = _cut(title, MAX_TITLE_CHARS + 40)
    room = MAX_MESSAGE_CHARS - len(title) - 1
    if len(body) > room:
        body = body[: room - len(ELLIPSIS)].rstrip() + ELLIPSIS
    return title, body


def render_match(match: ClaimedMatch) -> tuple[str, str]:
    """`🎯 0.62 Cohere — Machine Learning Intern` / `Terms: … · Locations: …` / URL."""
    title = f"🎯 {match.score:.2f} {_cut(match.company, 80)} — {_cut(match.title, MAX_TITLE_CHARS)}"
    facts = []
    if match.terms:
        facts.append("Terms: " + ", ".join(match.terms))
    if match.locations:
        facts.append("Locations: " + "; ".join(match.locations))
    lines = [_cut(" · ".join(facts), 600)] if facts else []
    lines.append(match.url.strip())
    return _fit(title, "\n".join(lines))


def render_digest(matches: list[ClaimedMatch]) -> tuple[str, str]:
    """One message for the overflow: `company — title — score` per line, then "and N more"."""
    title = f"🗂 {len(matches)} more radar match{'es' if len(matches) != 1 else ''}"
    lines = [
        f"{_cut(m.company, 60)} — {_cut(m.title, 120)} — {m.score:.2f}" for m in matches[:DIGEST_MAX_LINES]
    ]
    rest = len(matches) - DIGEST_MAX_LINES
    if rest > 0:
        lines.append(f"and {rest} more — run `orbit radar top`")
    return _fit(title, "\n".join(lines))


def build_events(matches: list[ClaimedMatch], max_individual: int) -> list[Event]:
    """One event per match for the first `max_individual`, the rest in one digest event."""
    events = []
    for match in matches[:max_individual]:
        title, body = render_match(match)
        events.append(Event(title=title, body=body, data={"posting_ids": [match.posting_id], "kind": KIND_MATCH}))
    overflow = matches[max_individual:]
    if overflow:
        title, body = render_digest(overflow)
        ids = [m.posting_id for m in overflow]
        events.append(Event(title=title, body=body, data={"posting_ids": ids, "kind": KIND_DIGEST}))
    return events


async def notify_matches(db: Database, config: RadarConfig, *, now: datetime | None = None) -> NotifySummary:
    """The pipeline's notify stage: claim, then render events. Claims nothing when the cap is 0."""
    if config.max_notifications_per_poll <= 0:
        return NotifySummary(disabled=True)
    matches, new, expired = await _claim(db, config, now=now)
    events = build_events(matches, config.max_notifications_per_poll)
    individual = min(len(matches), config.max_notifications_per_poll)
    return NotifySummary(
        new=new,
        retried=len(matches) - new,
        expired=expired,
        matches=individual,
        digested=len(matches) - individual,
        events=events,
    )


def describe_notify(summary: NotifySummary) -> str:
    if summary.disabled:
        return "off (max_notifications_per_poll = 0)"
    text = f"claimed {summary.new} new, {summary.retried} retries; {summary.matches} messages"
    if summary.digested:
        text += f" + digest of {summary.digested}"
    if summary.expired:
        text += f"; expired {summary.expired} stale pending"
    return text


# ---- delivery result --------------------------------------------------------------------------------


async def record_delivery(
    db: Database, posting_ids: list[int], ok: bool, *, error: str = PUSH_FAILED_ERROR, now: datetime | None = None
) -> int:
    """`sent` (sent_at = now) or `failed` (attempts + 1, capped at MAX_ATTEMPTS) for these postings' rows.

    Rows already `sent` are left alone. A row an expiry pass marked failed meanwhile still records the
    real outcome (a confirmed delivery beats "unknown"). Returns the number of rows updated.
    """
    if not posting_ids:
        return 0
    stamp = _stamp(now)
    target = and_(Notification.posting_id.in_(list(posting_ids)), Notification.status != "sent")
    if ok:
        # An expired row already counts MAX_ATTEMPTS; don't push it past the cap.
        attempts = case((Notification.attempts >= MAX_ATTEMPTS, Notification.attempts), else_=Notification.attempts + 1)
        values: dict[str, Any] = {"status": "sent", "sent_at": stamp, "attempts": attempts, "error": None}
    else:
        values = {
            "status": "failed",
            "attempts": func.least(Notification.attempts + 1, MAX_ATTEMPTS),
            "error": error[:500],
        }
    async with db.session() as s:
        result = await s.execute(update(Notification).where(target).values(**values).returning(Notification.id))
        updated = len(result.all())
        await s.commit()
    return updated
