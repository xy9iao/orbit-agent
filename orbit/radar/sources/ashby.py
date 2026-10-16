"""Ashby Posting API: GET api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false (public, no auth)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from orbit.radar.models import NormalizedPosting, parse_iso
from orbit.radar.sources.base import CompanyLike
from orbit.radar.sources.http import PoliteClient, SourceFetchError
from orbit.radar.sources.text import html_to_text, join_locations, tidy_text

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board"


class AshbyAdapter:
    source = "ashby"

    def board_request(self, board_token: str) -> tuple[str, dict[str, Any]]:
        return f"{BASE_URL}/{quote(board_token.strip(), safe='')}", {"includeCompensation": "false"}

    async def fetch(self, client: PoliteClient, company: CompanyLike) -> list[NormalizedPosting]:
        url, params = self.board_request(company.board_token)
        data = await client.get_json(url, params)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise SourceFetchError(f"GET {url}: expected an object with a 'jobs' list", url=url)
        # Unlisted jobs are reachable by direct link only; the company doesn't advertise them, so neither do we.
        return [self.normalize(job, company.slug) for job in jobs if job.get("isListed", True) is not False]

    def normalize(self, job: dict[str, Any], company_slug: str) -> NormalizedPosting:
        secondary = job.get("secondaryLocations")
        secondary_names = [
            s.get("location") for s in (secondary if isinstance(secondary, list) else []) if isinstance(s, dict)
        ]
        work_mode = "remote" if job.get("isRemote") is True else job.get("workplaceType")
        department = (job.get("department") or job.get("team") or "").strip() or None
        return NormalizedPosting(
            source=self.source,
            external_id=str(job["id"]),
            company_slug=company_slug,
            title=(job.get("title") or "").strip(),
            url=job.get("jobUrl") or job.get("applyUrl") or "",
            location_raw=join_locations([job.get("location"), *secondary_names], work_mode),
            department=department,
            description_text=tidy_text(job.get("descriptionPlain"))
            or html_to_text(job.get("descriptionHtml"), unescape=False),
            posted_at=parse_iso(job.get("publishedAt")),
            updated_at=None,  # the public Posting API has no last-modified timestamp
            raw=job,
        )
