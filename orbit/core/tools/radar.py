"""Internship Radar chat tools (M6.2): search stored postings and set the radar goal.

Search rules (also in the `search_postings` docstring the model sees):
- Base set: open postings (closed too with `include_closed`). With `internship_only` (the default) only
  postings whose extraction succeeded and says `is_internship`; with it off, every posting, including
  ones not extracted yet.
- `term`, `location` and `role_type` read extracted fields, so they only ever match postings with
  `extraction_status = 'ok'`; unextracted postings are reachable through the keyword query only.
- term: the same rule as scoring (`match.term_filter`): a posting naming one of the terms, or naming no
  term at all, passes.
- location: literal, not the scoring rule. A city matches extracted locations containing it ("toronto"
  -> "Toronto, ON"); a province matches its cities; "Canada" matches any Canadian location plus remote
  roles open to Canada; "Remote" matches any "Remote (...)" location or `remote_ok`; "Remote (Canada)"
  matches that location or `remote_ok` + `canada_eligible`. A city search does not add remote roles.
- Ranking: an empty query sorts by stored score (unscored last), then newest first. A non-empty query
  with an `embed` provider is embedded and the filtered postings that have an embedding are ranked by
  cosine similarity (pgvector `<=>`); remaining slots are filled with keyword matches among filtered
  postings without an embedding. Without a provider, or if the embed call fails, the whole search is
  keyword ranked: whole-word, case-insensitive matches of the query words (title 3, company 2,
  description 1 per word), ties by score then recency.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import ColumnElement, and_, case, exists, func, literal, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from orbit.core.router import ModelRouter
from orbit.radar.match import GOAL_WEIGHT, RESUME_WEIGHT, internship_filter, term_filter
from orbit.radar.models import PROVINCES, RoleType, normalize_location, normalize_role_type, normalize_terms
from orbit.radar.profile import get_profile, set_goal
from orbit.storage.models import EMBEDDING_DIM, Company, Notification, Posting

from .registry import ToolContext, tool

EMBED_TASK = "embed"
DEFAULT_LIMIT = 10
MAX_LIMIT = 25
FILTER_KEYS = ("term", "location", "role_type", "since_days", "include_closed", "internship_only", "limit")
MAX_QUERY_WORDS = 8
# Words that say nothing about which posting is meant ("any new AI co-ops in Toronto?").
STOPWORDS = {
    "a", "an", "and", "any", "are", "at", "for", "from", "i", "in", "is", "job", "jobs", "me", "my", "new",
    "of", "on", "or", "posting", "postings", "role", "roles", "show", "some", "the", "there", "to", "with",
}  # fmt: skip
# Keyword weights per matched query word.
TITLE_WEIGHT, COMPANY_WEIGHT, DESCRIPTION_WEIGHT = 3, 2, 1


@dataclass(frozen=True)
class SearchFilters:
    terms: list[str]
    location: str | None  # canonical form when it could be normalized, else as written
    role_type: RoleType | None
    since_days: int | None
    include_closed: bool
    internship_only: bool
    limit: int

    def applied(self) -> dict[str, Any]:
        """The normalized filters, echoed to the model so it can explain what was searched."""
        out: dict[str, Any] = {}
        if self.terms:
            out["term"] = self.terms
        if self.location:
            out["location"] = self.location
        if self.role_type:
            out["role_type"] = self.role_type.value
        if self.since_days is not None:
            out["since_days"] = self.since_days
        out["include_closed"] = self.include_closed
        out["internship_only"] = self.internship_only
        out["limit"] = self.limit
        return out


class FilterError(ValueError):
    pass


def _as_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "1"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "no", "0"):
        return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise FilterError(f"{key} must be true or false, got {value!r}")


def _as_int(key: str, value: Any) -> int:
    if isinstance(value, bool):
        raise FilterError(f"{key} must be a whole number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise FilterError(f"{key} must be a whole number, got {value!r}") from None
    if number != int(number):
        raise FilterError(f"{key} must be a whole number, got {value!r}")
    return int(number)


def parse_filters(filters: dict[str, Any] | None) -> SearchFilters:
    """Validate and normalize the model's filters dict; FilterError names the bad key."""
    filters = dict(filters or {})
    unknown = sorted(set(filters) - set(FILTER_KEYS))
    if unknown:
        raise FilterError(f"unknown filter key(s) {unknown}; allowed: {', '.join(FILTER_KEYS)}")
    present = {k: v for k, v in filters.items() if v is not None and v != ""}

    terms: list[str] = []
    if "term" in present:
        raw = present["term"]
        try:
            terms = normalize_terms(raw if isinstance(raw, list) else [str(raw)])
        except ValueError:
            raise FilterError(f"cannot read term {raw!r}; use a form like 'Summer 2027' or 'Fall 2026'") from None

    location: str | None = None
    if "location" in present:
        raw = str(present["location"]).strip()
        try:
            location = normalize_location(raw)
        except ValueError:
            location = raw  # unknown text: matched as a plain substring

    role_type: RoleType | None = None
    if "role_type" in present:
        raw = str(present["role_type"])
        try:
            role_type = normalize_role_type(raw)
        except ValueError:
            raise FilterError("role_type is empty") from None
        if role_type is RoleType.other and raw.strip().lower() != "other":
            values = ", ".join(r.value for r in RoleType)
            raise FilterError(f"unknown role_type {raw!r}; use one of: {values}")

    since_days: int | None = None
    if "since_days" in present:
        since_days = _as_int("since_days", present["since_days"])
        if since_days < 1:
            raise FilterError("since_days must be 1 or more")

    limit = DEFAULT_LIMIT
    if "limit" in present:
        limit = max(1, min(MAX_LIMIT, _as_int("limit", present["limit"])))

    return SearchFilters(
        terms=terms,
        location=location,
        role_type=role_type,
        since_days=since_days,
        include_closed=_as_bool("include_closed", present.get("include_closed", False)),
        internship_only=_as_bool("internship_only", present.get("internship_only", True)),
        limit=limit,
    )


# ---- SQL conditions ---------------------------------------------------------------------------------


def _flag(key: str) -> ColumnElement[bool]:
    return Posting.extraction[key].astext == "true"


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _any_location_like(pattern: str) -> ColumnElement[bool]:
    """Some extracted location ILIKE `pattern` (already escaped)."""
    loc = func.jsonb_array_elements_text(Posting.extraction["locations"]).table_valued("value")
    return exists(select(literal(1)).select_from(loc).where(loc.c.value.ilike(pattern, escape="\\")))


def location_condition(location: str) -> ColumnElement[bool]:
    """Literal location match over canonical extracted locations; see the module docstring."""
    remote_canada = and_(_flag("remote_ok"), _flag("canada_eligible"))
    if location == "Remote":
        return or_(_any_location_like("Remote%"), _flag("remote_ok"))
    if location == "Remote (Canada)":
        return or_(_any_location_like("Remote (Canada)"), remote_canada)
    if location.startswith("Remote ("):
        return _any_location_like(_like_escape(location))
    if location == "Canada":
        patterns = [f"%, {code}" for code in PROVINCES] + ["%, Canada", "Canada", "Remote (Canada)"]
        return or_(*(_any_location_like(p) for p in patterns), remote_canada)
    province = next((code for code, name in PROVINCES.items() if location == f"{name}, Canada"), None)
    if province:
        return or_(_any_location_like(f"%, {province}"), _any_location_like(_like_escape(location)))
    city = location.split(",")[0].strip() or location
    return _any_location_like(f"%{_like_escape(city)}%")


def filter_condition(f: SearchFilters, now: datetime) -> ColumnElement[bool]:
    conds: list[ColumnElement[bool]] = []
    if not f.include_closed:
        conds.append(Posting.status == "open")
    if f.internship_only:
        conds += [Posting.extraction_status == "ok", _flag("is_internship")]
    if f.terms or f.location or f.role_type:
        conds.append(Posting.extraction_status == "ok")  # extracted-field filters need an ok extraction
    if f.terms:
        conds.append(term_filter(f.terms))
    if f.location:
        conds.append(location_condition(f.location))
    if f.role_type:
        conds.append(Posting.extraction["role_type"].astext == f.role_type.value)
    if f.since_days is not None:
        conds.append(Posting.first_seen_at >= now - timedelta(days=f.since_days))
    return and_(true(), *conds)


def query_words(query: str) -> list[str]:
    """Lowercased query words worth matching: stopwords dropped, deduped, at most MAX_QUERY_WORDS."""
    out: list[str] = []
    for word in re.findall(r"[\w][\w+#.\-]*", query.lower()):
        word = word.rstrip(".-")
        if word and word not in STOPWORDS and word not in out:
            out.append(word)
    return out[:MAX_QUERY_WORDS]


def _word_regex(word: str) -> str:
    # Postgres ARE: a backslash before a non-alphanumeric char is that literal char, so re.escape is safe.
    return f"(^|[^[:alnum:]_]){re.escape(word)}([^[:alnum:]_]|$)"


def keyword_score(words: list[str]) -> ColumnElement[int]:
    """Weighted count of query words found as whole words in title, company name and description."""
    total: ColumnElement[int] = literal(0)
    for word in words:
        pattern = _word_regex(word)
        total = (
            total
            + case((Posting.title.regexp_match(pattern, flags="i"), TITLE_WEIGHT), else_=0)
            + case((Company.name.regexp_match(pattern, flags="i"), COMPANY_WEIGHT), else_=0)
            + case((Posting.description_text.regexp_match(pattern, flags="i"), DESCRIPTION_WEIGHT), else_=0)
        )
    return total


def _base_select(*extra: Any):
    notified = exists().where(Notification.posting_id == Posting.id, Notification.status == "sent").label("notified")
    return select(
        Posting.id,
        Company.name.label("company"),
        Posting.title,
        Posting.url,
        Posting.status,
        Posting.location_raw,
        Posting.extraction,
        Posting.extraction_status,
        Posting.score,
        Posting.first_seen_at,
        notified,
        *extra,
    ).join(Company, Company.id == Posting.company_id)


_BY_SCORE = (Posting.score.desc().nulls_last(), Posting.first_seen_at.desc(), Posting.id.desc())


def _row_dict(row: Any, f: SearchFilters, similarity: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"company": row.company, "title": row.title}
    extraction = row.extraction if row.extraction_status == "ok" and isinstance(row.extraction, dict) else None
    if extraction is not None:
        out["terms"] = [str(t) for t in extraction.get("terms") or []]
        out["locations"] = [str(loc) for loc in extraction.get("locations") or []]
        out["remote_ok"] = bool(extraction.get("remote_ok"))
        out["role_type"] = extraction.get("role_type")
        if not f.internship_only:
            out["internship"] = bool(extraction.get("is_internship"))
    else:
        out["extraction"] = row.extraction_status  # pending / failed / skipped: fields unknown
        out["location_raw"] = row.location_raw
    out["score"] = round(row.score, 3) if row.score is not None else None
    if similarity is not None:
        out["similarity"] = round(similarity, 3)
    out["first_seen"] = row.first_seen_at.date().isoformat()
    out["status"] = row.status
    out["url"] = row.url
    out["notified"] = bool(row.notified)
    return out


# ---- search -----------------------------------------------------------------------------------------


def embed_available(router: ModelRouter | None) -> bool:
    """True when config has an `embed` task whose provider is configured (has an API key)."""
    if router is None or EMBED_TASK not in router.config.tasks:
        return False
    try:
        router.resolve(EMBED_TASK)
    except Exception:  # noqa: BLE001 - missing provider means "not available"
        return False
    return True


async def _embed_query(router: ModelRouter, query: str) -> list[float]:
    [vector] = await router.embed(EMBED_TASK, [query])
    if len(vector) != EMBEDDING_DIM:
        raise RuntimeError(f"query embedding has {len(vector)} dims, expected {EMBEDDING_DIM}")
    return vector


async def _keyword_rows(
    s: AsyncSession, cond: ColumnElement[bool], words: list[str], limit: int, f: SearchFilters
) -> list[dict[str, Any]]:
    kw = keyword_score(words).label("kw")
    stmt = _base_select(kw).where(cond, keyword_score(words) > 0).order_by(kw.desc(), *_BY_SCORE).limit(limit)
    return [_row_dict(r, f) for r in (await s.execute(stmt)).all()]


async def _empty_message(s: AsyncSession, f: SearchFilters, has_query: bool) -> str:
    total = await s.scalar(select(func.count(Posting.id)))
    if not total:
        return (
            "No postings stored yet. Run `orbit radar seed` and then `orbit radar poll` "
            "(or wait for the internship_radar watcher's next poll)."
        )
    if f.internship_only:
        internships = await s.scalar(select(func.count(Posting.id)).where(internship_filter()))
        if not internships:
            return (
                f"{total} postings are stored but none is an extracted open internship yet; extraction runs "
                "with each poll (`orbit radar extract`). Try internship_only=false to search all postings."
            )
    options = "drop since_days, term or location, or set include_closed=true"
    if f.internship_only:
        options += " or internship_only=false"
    words = ", or use different query words" if has_query else ""
    return f"No postings match. Try relaxing the filters ({options}){words}."


async def search(
    db: Any, router: ModelRouter | None, query: str, f: SearchFilters, *, now: datetime | None = None
) -> dict[str, Any]:
    """Run one search; returns the header dict described in the module docstring."""
    now = now or datetime.now()
    query = (query or "").strip()
    words = query_words(query)
    cond = filter_condition(f, now)
    note: str | None = None

    vector: list[float] | None = None
    if query and embed_available(router):
        try:
            vector = await _embed_query(router, query)  # no DB session is open during the network call
        except Exception as e:  # noqa: BLE001 - fall back to keyword ranking
            logger.warning("search_postings: query embedding failed: {}", e)
            note = f"semantic search unavailable ({type(e).__name__}); used keyword matching"

    async with db.session() as s:
        if vector is not None:
            ranking = "semantic"
            distance = Posting.embedding.cosine_distance(vector)
            stmt = (
                _base_select(distance.label("distance"))
                .where(cond, Posting.embedding.is_not(None))
                .order_by(distance, Posting.id.desc())
                .limit(f.limit)
            )
            results = [_row_dict(r, f, 1 - float(r.distance)) for r in (await s.execute(stmt)).all()]
            if len(results) < f.limit and words:
                rest = and_(cond, Posting.embedding.is_(None))
                results += await _keyword_rows(s, rest, words, f.limit - len(results), f)
        elif words:
            ranking = "keyword"
            results = await _keyword_rows(s, cond, words, f.limit, f)
        else:
            ranking = "score"
            if query:
                note = "the query had no searchable words; listed by score instead"
            stmt = _base_select().where(cond).order_by(*_BY_SCORE).limit(f.limit)
            results = [_row_dict(r, f) for r in (await s.execute(stmt)).all()]
        message = None if results else await _empty_message(s, f, bool(words))

    out: dict[str, Any] = {"count": len(results), "ranking": ranking, "filters": f.applied()}
    if note:
        out["note"] = note
    if message:
        out["message"] = message
    out["results"] = results
    return out


@tool
async def search_postings(ctx: ToolContext, query: str = "", filters: dict | None = None) -> str:
    """Search the Internship Radar's stored job postings (internships/co-ops at Canadian tech companies).
    Use it for questions like "any new AI co-ops in Toronto?" or "what Summer 2027 roles are open at Shopify?".
    Returns JSON: count, ranking (semantic = by meaning, keyword = word matches, score = best resume match
    first), the normalized filters, and results with company, title, terms, locations, remote_ok, role_type,
    score (0-1 resume match, null if unscored), first_seen, status, url and notified (already pushed to the
    user). Only state what the results say, and include the url when listing a posting.

    Args:
        query: Free-text description of what to look for, e.g. "machine learning co-op" or "rust backend".
            Leave empty to list the best-scoring postings. Put places, terms and role categories in filters
            rather than in the query.
        filters: Optional object with any of these keys.
            term: work term, e.g. "Summer 2027" (or a list of terms); postings that name no term also match.
            location: a city, province or "Remote", e.g. "Toronto" (matches "Toronto, ON"), "Ontario",
            "Canada", "Remote" or "Remote (Canada)".
            role_type: one of swe, ml_ai, data, infra, security, mobile, frontend, hardware, product,
            design, research, other; common names like "AI", "machine learning" or "devops" also work.
            since_days: only postings first seen within this many days (use for "new" postings, e.g. 7).
            include_closed: also return postings that were taken down (default false).
            internship_only: only postings identified as internships/co-ops (default true); set false to
            search every stored posting, including ones not analyzed yet (those match the query only).
            limit: how many results (default 10, max 25).
    """
    try:
        f = parse_filters(filters)
    except FilterError as e:
        return f"error: {e}"
    if not isinstance(query, str):
        return "error: query must be a string"
    result = await search(ctx.db, ctx.router, query, f)
    return json.dumps(result, ensure_ascii=False)


@tool
async def set_radar_goal(ctx: ToolContext, text: str) -> str:
    """Set the user's internship goal for the Internship Radar, e.g. "ML infrastructure co-op in Toronto,
    Summer 2027, ideally LLM serving". It is embedded and blended into every posting's match score, so it
    shifts which postings get recommended and pushed. Replaces any previous goal.

    Args:
        text: The goal in the user's own words: kind of role, topics, places, terms.
    """
    text = (text or "").strip()
    if not text:
        return "error: the goal text is empty; ask the user what kind of internship they want"
    if not embed_available(ctx.router):
        return (
            "error: no embedding provider is configured (the 'embed' task needs OPENAI_API_KEY), "
            "so the goal was not saved"
        )
    result = await set_goal(ctx.db, ctx.router, text)
    verb = "saved" if result.changed else "unchanged (same text as the current goal)"
    reply = (
        f"radar goal {verb}: {text!r}. It counts for {GOAL_WEIGHT:.0%} of each match score "
        f"(resume {RESUME_WEIGHT:.0%}) starting with the next radar poll."
    )
    resume = await get_profile(ctx.db, "resume")
    if resume is None or resume.embedding is None:
        reply += " Note: no resume is embedded yet, so nothing is scored until `orbit radar resume` runs."
    return reply


__all__ = ["search_postings", "set_radar_goal", "search", "parse_filters", "SearchFilters"]
