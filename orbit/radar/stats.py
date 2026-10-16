"""`orbit radar stats` (M6.3): the radar's resume metrics, read straight from Postgres.

CLAUDE.md lists the metrics: companies tracked, postings stored, extraction accuracy per field,
posting-to-notification latency and uptime days. Everything here is read-only and needs no API key.

- Accuracy comes from the last `orbit radar eval` summary in kv `radar:last_eval` (M4.5); its UNVERIFIED
  flag is carried through so provisional numbers are never shown without the warning.
- Latency is `sent_at - first_seen_at` over `status = 'sent'` notifications only (M6.1): failed and pending
  rows have no delivery time.
- Uptime counts distinct calendar days (naive local time, like every Orbit timestamp) with at least one
  `ok IS TRUE` poll run; failed and unfinished (`ok IS NULL`) runs never count (M3.2).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Date, cast, distinct, extract, func, select

from orbit.radar.eval import FIELDS as EVAL_FIELDS, KV_LAST_EVAL, UNVERIFIED_BANNER
from orbit.storage.db import Database
from orbit.storage.models import RADAR_SOURCES, Company, Notification, Posting, Run, Usage

# Router tasks whose usage is the radar's model cost (config.yaml `tasks`).
RADAR_TASKS = ("extract", "embed")


@dataclass
class SourceCount:
    total: int = 0
    active: int = 0


@dataclass
class CompanyStats:
    total: int = 0
    active: int = 0
    verified_ok: int = 0  # active companies whose last verify-sources check passed
    by_source: dict[str, SourceCount] = field(default_factory=dict)


@dataclass
class PostingStats:
    total: int = 0
    open: int = 0
    closed: int = 0
    extraction: dict[str, int] = field(default_factory=dict)  # pending/ok/failed/skipped over all stored postings
    internships: int = 0  # extraction ok and is_internship, open or closed
    embedded: int = 0  # embedding current (embedded_hash = content_hash)
    scored: int = 0  # open postings with a score (what `orbit radar top` ranks)


@dataclass
class FieldAccuracy:
    name: str
    accuracy: float
    n: int
    mean_jaccard: float | None = None


@dataclass
class EvalStats:
    at: str | None
    model: str | None
    label_file: str | None
    unverified: bool
    n: int
    failures: int
    fields: list[FieldAccuracy]


@dataclass
class LatencyStats:
    sent: int = 0
    failed: int = 0
    pending: int = 0
    median_seconds: float | None = None
    p90_seconds: float | None = None


@dataclass
class UptimeStats:
    days: int = 0
    first_ok_at: datetime | None = None
    last_ok_at: datetime | None = None
    runs_24h_ok: int = 0
    runs_24h_failed: int = 0


@dataclass
class TaskUsage:
    task: str
    calls: int
    prompt_tokens: int
    completion_tokens: int


@dataclass
class RadarStats:
    generated_at: datetime
    companies: CompanyStats
    postings: PostingStats
    eval: EvalStats | None  # None: no eval run yet
    latency: LatencyStats
    uptime: UptimeStats
    usage_today: list[TaskUsage]

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready (datetimes as ISO strings) for `orbit radar stats --json`."""
        return _jsonable(asdict(self))


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


async def _company_stats(s) -> CompanyStats:
    rows = (
        await s.execute(
            select(
                Company.source,
                func.count(),
                func.count().filter(Company.active.is_(True)),
                func.count().filter(Company.active.is_(True), Company.last_verify_status == "ok"),
            ).group_by(Company.source)
        )
    ).all()
    stats = CompanyStats(by_source={src: SourceCount() for src in RADAR_SOURCES})
    for source, total, active, verified in rows:
        stats.by_source[source] = SourceCount(total=int(total), active=int(active))
        stats.total += int(total)
        stats.active += int(active)
        stats.verified_ok += int(verified)
    return stats


async def _posting_stats(s) -> PostingStats:
    internship = Posting.extraction["is_internship"].astext == "true"
    row = (
        await s.execute(
            select(
                func.count(),
                func.count().filter(Posting.status == "open"),
                func.count().filter(Posting.status == "closed"),
                func.count().filter(Posting.extraction_status == "ok", internship),
                func.count().filter(Posting.embedded_hash == Posting.content_hash),
                func.count().filter(Posting.status == "open", Posting.score.is_not(None)),
            )
        )
    ).one()
    by_status = dict(
        (await s.execute(select(Posting.extraction_status, func.count()).group_by(Posting.extraction_status))).all()
    )
    return PostingStats(
        total=int(row[0]),
        open=int(row[1]),
        closed=int(row[2]),
        extraction={k: int(by_status.get(k, 0)) for k in ("ok", "failed", "skipped", "pending")},
        internships=int(row[3]),
        embedded=int(row[4]),
        scored=int(row[5]),
    )


def _eval_stats(value: Any) -> EvalStats | None:
    """Read the kv summary defensively: a hand-edited or older value must not crash `stats`."""
    if not isinstance(value, dict):
        return None
    fields = []
    raw_fields = value.get("fields")
    for name, f in (raw_fields.items() if isinstance(raw_fields, dict) else []):
        if not isinstance(f, dict) or not isinstance(f.get("accuracy"), int | float):
            continue
        jac = f.get("mean_jaccard")
        fields.append(
            FieldAccuracy(
                name=str(name),
                accuracy=float(f["accuracy"]),
                n=int(f.get("n") or 0),
                mean_jaccard=float(jac) if isinstance(jac, int | float) else None,
            )
        )
    # JSONB does not keep key order: report in PostingExtraction's field order, like `orbit radar eval`.
    order = {name: i for i, name in enumerate(EVAL_FIELDS)}
    fields.sort(key=lambda f: (order.get(f.name, len(order)), f.name))
    return EvalStats(
        at=value.get("at"),
        model=value.get("model"),
        label_file=value.get("label_file"),
        # Missing flag = provisional: never present numbers as verified unless the summary says so.
        unverified=bool(value.get("unverified", True)),
        n=int(value.get("n") or 0),
        failures=int(value.get("failures") or 0),
        fields=fields,
    )


async def _latency_stats(s) -> LatencyStats:
    counts = dict((await s.execute(select(Notification.status, func.count()).group_by(Notification.status))).all())
    seconds = extract("epoch", Notification.sent_at - Posting.first_seen_at)
    median, p90 = (
        await s.execute(
            select(
                func.percentile_cont(0.5).within_group(seconds),
                func.percentile_cont(0.9).within_group(seconds),
            )
            .select_from(Notification)
            .join(Posting, Posting.id == Notification.posting_id)
            .where(Notification.status == "sent", Notification.sent_at.is_not(None))
        )
    ).one()
    return LatencyStats(
        sent=int(counts.get("sent", 0)),
        failed=int(counts.get("failed", 0)),
        pending=int(counts.get("pending", 0)),
        median_seconds=float(median) if median is not None else None,
        p90_seconds=float(p90) if p90 is not None else None,
    )


async def _uptime_stats(s, now: datetime) -> UptimeStats:
    ok = Run.ok.is_(True)
    days, first, last = (
        await s.execute(
            select(func.count(distinct(cast(Run.started_at, Date))), func.min(Run.started_at), func.max(Run.started_at))
            .where(ok)
        )
    ).one()
    recent_ok, recent_failed = (
        await s.execute(
            select(func.count().filter(ok), func.count().filter(Run.ok.is_(False))).where(
                Run.started_at > now - timedelta(hours=24), Run.started_at <= now
            )
        )
    ).one()
    return UptimeStats(
        days=int(days),
        first_ok_at=first,
        last_ok_at=last,
        runs_24h_ok=int(recent_ok),
        runs_24h_failed=int(recent_failed),
    )


async def _usage_today(s, now: datetime) -> list[TaskUsage]:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        await s.execute(
            select(
                Usage.task,
                func.count(),
                func.coalesce(func.sum(Usage.prompt_tokens), 0),
                func.coalesce(func.sum(Usage.completion_tokens), 0),
            )
            .where(Usage.task.in_(RADAR_TASKS), Usage.created_at >= start, Usage.created_at <= now)
            .group_by(Usage.task)
        )
    ).all()
    found = {task: TaskUsage(task, int(c), int(p), int(o)) for task, c, p, o in rows}
    return [found.get(task, TaskUsage(task, 0, 0, 0)) for task in RADAR_TASKS]


async def collect_stats(db: Database, *, now: datetime | None = None) -> RadarStats:
    """Every metric in one read-only pass. `now` (naive local) anchors the 24 h window and "today"."""
    now = now or _now()
    async with db.session() as s:
        companies = await _company_stats(s)
        postings = await _posting_stats(s)
        latency = await _latency_stats(s)
        uptime = await _uptime_stats(s, now)
        usage = await _usage_today(s, now)
    eval_stats = _eval_stats(await db.kv_get(KV_LAST_EVAL))
    return RadarStats(
        generated_at=now,
        companies=companies,
        postings=postings,
        eval=eval_stats,
        latency=latency,
        uptime=uptime,
        usage_today=usage,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    """Compact human duration: 45s, 12m 05s, 3h 07m, 2d 04h."""
    total = max(0, round(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _ts(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "never"


def _eval_lines(ev: EvalStats | None) -> list[str]:
    lines = ["Extraction accuracy (last `orbit radar eval`)"]
    if ev is None:
        return [*lines, "  no eval run yet (run `orbit radar eval`)"]
    if ev.unverified:
        lines.append(f"  {UNVERIFIED_BANNER}")
    lines.append(
        f"  run {ev.at or 'unknown'}, model {ev.model or 'unknown'}, {ev.n} items, {ev.failures} failed extractions"
    )
    if ev.label_file:
        lines.append(f"  labels: {ev.label_file}")
    if not ev.fields:
        return [*lines, "  no per-field scores stored"]
    lines.append(f"  {'field':<16} {'accuracy':>9} {'jaccard':>8} {'n':>4}")
    for f in ev.fields:
        jac = f"{f.mean_jaccard:.2f}" if f.mean_jaccard is not None else "-"
        lines.append(f"  {f.name:<16} {f.accuracy * 100:>8.1f}% {jac:>8} {f.n:>4}")
    return lines


def format_stats(stats: RadarStats) -> str:
    """Plain-text sections; pass the result through cli._printable before echoing (the banner's warning sign)."""
    c, p, lat, up = stats.companies, stats.postings, stats.latency, stats.uptime
    per_source = ", ".join(f"{src} {sc.active}/{sc.total}" for src, sc in c.by_source.items())
    ex = p.extraction
    lines = [
        f"Internship Radar stats ({stats.generated_at:%Y-%m-%d %H:%M})",
        "",
        "Companies",
        f"  tracked: {c.active} active of {c.total}; {c.verified_ok} active with a passing verify-sources check",
        f"  by source (active/total): {per_source}",
        "",
        "Postings",
        f"  stored: {p.total} ({p.open} open, {p.closed} closed)",
        f"  extraction: {ex.get('ok', 0)} ok, {ex.get('failed', 0)} failed, {ex.get('skipped', 0)} skipped "
        f"(prefilter), {ex.get('pending', 0)} pending",
        f"  internships: {p.internships}; embedded (current): {p.embedded}; open and scored: {p.scored}",
        "",
        *_eval_lines(stats.eval),
        "",
        "Posting-to-notification latency (sent_at - first_seen_at)",
    ]
    if lat.median_seconds is None or lat.p90_seconds is None:
        lines.append("  none sent yet")
    else:
        lines.append(
            f"  median {format_duration(lat.median_seconds)}, p90 {format_duration(lat.p90_seconds)} "
            f"over {lat.sent} sent"
        )
    lines.append(f"  notifications: {lat.sent} sent, {lat.failed} failed, {lat.pending} pending")
    lines += [
        "",
        "Uptime",
        f"  {up.days} day{'s' if up.days != 1 else ''} with at least one successful poll",
        f"  first successful poll: {_ts(up.first_ok_at)}; last: {_ts(up.last_ok_at)}",
        f"  company polls in the last 24 h: {up.runs_24h_ok} ok, {up.runs_24h_failed} failed",
        "",
        "Model usage today (radar tasks)",
    ]
    for u in stats.usage_today:
        lines.append(
            f"  {u.task}: {u.calls} calls, {u.prompt_tokens} prompt + {u.completion_tokens} completion tokens"
        )
    if stats.eval is not None and stats.eval.unverified:
        lines += ["", UNVERIFIED_BANNER]
    return "\n".join(lines)


__all__ = [
    "RADAR_TASKS",
    "CompanyStats",
    "EvalStats",
    "FieldAccuracy",
    "LatencyStats",
    "PostingStats",
    "RadarStats",
    "SourceCount",
    "TaskUsage",
    "UptimeStats",
    "collect_stats",
    "format_duration",
    "format_stats",
]
