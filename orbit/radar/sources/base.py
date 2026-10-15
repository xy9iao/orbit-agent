"""The source-adapter contract: one adapter per public job-board API."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from orbit.radar.models import NormalizedPosting
from orbit.radar.sources.http import PoliteClient


class CompanyLike(Protocol):
    """Anything with a slug and a board token; the ORM `Company` satisfies it."""

    slug: str
    board_token: str


@runtime_checkable
class SourceAdapter(Protocol):
    source: str

    def board_request(self, board_token: str) -> tuple[str, dict[str, Any]]:
        """The (url, query params) that list every open posting on one board."""
        ...

    async def fetch(self, client: PoliteClient, company: CompanyLike) -> list[NormalizedPosting]:
        """Fetch one board and normalize every posting; raises `SourceFetchError` on failure."""
        ...
