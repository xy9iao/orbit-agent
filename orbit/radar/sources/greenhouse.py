"""Greenhouse Job Board API: GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true (public, no auth)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from orbit.radar.models import NormalizedPosting, parse_iso
from orbit.radar.sources.base import CompanyLike
from orbit.radar.sources.http import PoliteClient, SourceFetchError
from orbit.radar.sources.text import html_to_text

BASE_URL = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseAdapter:
    source = "greenhouse"

    def board_request(self, board_token: str) -> tuple[str, dict[str, Any]]:
        return f"{BASE_URL}/{quote(board_token, safe='')}/jobs", {"content": "true"}

    async def fetch(self, client: PoliteClient, company: CompanyLike) -> list[NormalizedPosting]:
        url, params = self.board_request(company.board_token)
        data = await client.get_json(url, params)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise SourceFetchError(f"GET {url}: expected an object with a 'jobs' list", url=url)
        return [self.normalize(job, company.slug) for job in jobs]

    def normalize(self, job: dict[str, Any], company_slug: str) -> NormalizedPosting:
        location = job.get("location")
        location_name = location.get("name") if isinstance(location, dict) else None
        departments = job.get("departments") or []
        department = next((d["name"] for d in departments if isinstance(d, dict) and d.get("name")), None)
        return NormalizedPosting(
            source=self.source,
            external_id=str(job["id"]),
            company_slug=company_slug,
            title=(job.get("title") or "").strip(),
            url=job.get("absolute_url") or "",
            location_raw=(location_name or "").strip(),
            department=department,
            description_text=html_to_text(job.get("content")),
            posted_at=parse_iso(job.get("first_published")),
            updated_at=parse_iso(job.get("updated_at")),
            raw=job,
        )
