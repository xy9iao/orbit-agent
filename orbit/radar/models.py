"""Pydantic models shared across the radar pipeline.

`NormalizedPosting` is what every source adapter returns: one shape regardless of job board.
(M4.1 adds `PostingExtraction`, the LLM-extracted fields, to this module.)
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
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
