"""Lever Postings API: GET api.lever.co/v0/postings/{token}?mode=json (public, no auth).

EU-hosted Lever accounts live on api.eu.lever.co. A board token written as `eu:<site>` (for example
`eu:acme`) is fetched from the EU host; any other token uses the global host.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from orbit.radar.models import NormalizedPosting
from orbit.radar.sources.base import CompanyLike
from orbit.radar.sources.http import PoliteClient, SourceFetchError
from orbit.radar.sources.text import html_to_text, join_locations, tidy_text

BASE_URL = "https://api.lever.co/v0/postings"
EU_BASE_URL = "https://api.eu.lever.co/v0/postings"
EU_PREFIX = "eu:"


def split_board_token(board_token: str) -> tuple[str, str]:
    """`eu:acme` -> (EU base URL, "acme"); `acme` -> (global base URL, "acme")."""
    token = board_token.strip()
    if token[: len(EU_PREFIX)].lower() == EU_PREFIX:
        return EU_BASE_URL, token[len(EU_PREFIX) :].strip()
    return BASE_URL, token


def epoch_ms_to_datetime(value: Any) -> datetime | None:
    """Lever timestamps are epoch milliseconds; returns naive local time, or None if absent/garbled."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000)
    except (OverflowError, OSError, ValueError):
        return None


class LeverAdapter:
    source = "lever"

    def board_request(self, board_token: str) -> tuple[str, dict[str, Any]]:
        base, site = split_board_token(board_token)
        return f"{base}/{quote(site, safe='')}", {"mode": "json"}

    async def fetch(self, client: PoliteClient, company: CompanyLike) -> list[NormalizedPosting]:
        url, params = self.board_request(company.board_token)
        data = await client.get_json(url, params)
        if not isinstance(data, list):
            raise SourceFetchError(f"GET {url}: expected a list of postings", url=url)
        return [self.normalize(job, company.slug) for job in data]

    def normalize(self, job: dict[str, Any], company_slug: str) -> NormalizedPosting:
        categories = job.get("categories") if isinstance(job.get("categories"), dict) else {}
        all_locations = categories.get("allLocations")
        locations = [categories.get("location"), *(all_locations if isinstance(all_locations, list) else [])]
        department = (categories.get("department") or categories.get("team") or "").strip() or None
        return NormalizedPosting(
            source=self.source,
            external_id=str(job["id"]),
            company_slug=company_slug,
            title=(job.get("text") or "").strip(),
            url=job.get("hostedUrl") or job.get("applyUrl") or "",
            location_raw=join_locations(locations, job.get("workplaceType")),
            department=department,
            description_text=self.description(job),
            posted_at=epoch_ms_to_datetime(job.get("createdAt")),
            updated_at=None,  # the public Postings API has no last-modified timestamp
            raw=job,
        )

    @staticmethod
    def description(job: dict[str, Any]) -> str:
        """Intro (`descriptionPlain`), then each list as its heading plus bullets, then `additionalPlain`."""
        parts = [tidy_text(job.get("descriptionPlain")) or html_to_text(job.get("description"), unescape=False)]
        for item in job.get("lists") or []:
            if not isinstance(item, dict):
                continue
            heading = tidy_text(item.get("text"))
            body = html_to_text(item.get("content"), unescape=False)
            parts.append("\n".join(p for p in (heading, body) if p))
        parts.append(tidy_text(job.get("additionalPlain")) or html_to_text(job.get("additional"), unescape=False))
        return "\n\n".join(p for p in parts if p)
