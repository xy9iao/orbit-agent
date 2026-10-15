"""Job-board source adapters (Greenhouse, Lever, Ashby), all public no-login APIs."""

from __future__ import annotations

from orbit.radar.sources.base import CompanyLike, SourceAdapter
from orbit.radar.sources.greenhouse import GreenhouseAdapter
from orbit.radar.sources.http import PoliteClient, SourceFetchError
from orbit.radar.sources.lever import LeverAdapter

_ADAPTERS: dict[str, SourceAdapter] = {
    GreenhouseAdapter.source: GreenhouseAdapter(),
    LeverAdapter.source: LeverAdapter(),
}


def get_adapter(source: str) -> SourceAdapter:
    try:
        return _ADAPTERS[source]
    except KeyError:
        raise ValueError(f"unknown radar source {source!r}; known: {', '.join(sorted(_ADAPTERS))}") from None


def available_sources() -> list[str]:
    return sorted(_ADAPTERS)


__all__ = [
    "CompanyLike",
    "GreenhouseAdapter",
    "LeverAdapter",
    "PoliteClient",
    "SourceAdapter",
    "SourceFetchError",
    "available_sources",
    "get_adapter",
]
