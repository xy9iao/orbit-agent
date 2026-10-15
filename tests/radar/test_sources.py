"""Source adapters and the polite HTTP client, all through httpx.MockTransport (never the live network)."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from orbit.radar.models import NormalizedPosting
from orbit.radar.sources import SourceAdapter, get_adapter
from orbit.radar.sources.greenhouse import GreenhouseAdapter
from orbit.radar.sources.http import USER_AGENT, PoliteClient, SourceFetchError, parse_retry_after
from orbit.radar.sources.text import html_to_text

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "radar"
GREENHOUSE_FIXTURE = FIXTURES / "greenhouse_faire.json"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any request that escapes a MockTransport fails loudly instead of reaching the internet."""

    async def _refuse(self, request):
        raise AssertionError(f"test tried to reach the network: {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _refuse)


class FakeTime:
    """Injectable clock + sleep: sleeping advances the clock and is recorded, but returns immediately."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class FakeCompany:
    slug: str
    board_token: str


def make_client(handler, fake: FakeTime | None = None, **kwargs) -> PoliteClient:
    fake = fake or FakeTime()
    return PoliteClient(transport=httpx.MockTransport(handler), clock=fake.clock, sleep=fake.sleep, **kwargs)


def load_greenhouse() -> dict:
    return json.loads(GREENHOUSE_FIXTURE.read_text(encoding="utf-8"))


def greenhouse_handler(payload: dict, seen: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.url.host == "boards-api.greenhouse.io"
        assert request.url.path == "/v1/boards/faire/jobs"
        assert request.url.params.get("content") == "true"
        return httpx.Response(200, json=payload)

    return handler


# --- Greenhouse adapter ---------------------------------------------------------------------------


async def test_greenhouse_parses_recorded_fixture():
    payload = load_greenhouse()
    seen: list[httpx.Request] = []
    async with make_client(greenhouse_handler(payload, seen)) as client:
        postings = await GreenhouseAdapter().fetch(client, FakeCompany(slug="faire", board_token="faire"))

    jobs = payload["jobs"]
    assert len(seen) == 1
    assert len(postings) == len(jobs) == 5
    for posting, job in zip(postings, jobs, strict=True):
        assert isinstance(posting, NormalizedPosting)
        assert posting.source == "greenhouse"
        assert posting.company_slug == "faire"
        assert isinstance(posting.external_id, str) and posting.external_id == str(job["id"])
        assert posting.title == job["title"]
        assert posting.url == job["absolute_url"] and posting.url.startswith("https://")
        assert posting.location_raw == job["location"]["name"]
        assert posting.department == job["departments"][0]["name"]
        assert posting.raw == job  # raw is the API's job object, untouched
        # HTML-escaped HTML became readable text: no tags, no leftover entities.
        text = posting.description_text
        assert len(text) > 200
        assert not re.search(r"</?[a-zA-Z][^>]*>", text)
        assert not re.search(r"&(lt|gt|amp|quot|nbsp|#\d+);", text)
        assert "\n" in text  # paragraph breaks survive
        # Timestamps are naive local time.
        assert posting.updated_at is not None and posting.updated_at.tzinfo is None
        expected = datetime.fromisoformat(job["updated_at"]).astimezone().replace(tzinfo=None, microsecond=0)
        assert posting.updated_at == expected
        assert posting.posted_at is not None and posting.posted_at.tzinfo is None

    assert len({p.external_id for p in postings}) == len(postings)
    assert any("intern" in p.title.lower() for p in postings)


def test_greenhouse_missing_fields_fall_back():
    job = copy.deepcopy(load_greenhouse()["jobs"][0])
    job["location"] = None
    job["departments"] = []
    job["content"] = None
    del job["updated_at"]
    del job["first_published"]
    posting = GreenhouseAdapter().normalize(job, "faire")
    assert posting.location_raw == ""
    assert posting.department is None
    assert posting.description_text == ""
    assert posting.updated_at is None and posting.posted_at is None

    del job["location"]
    job["departments"] = [{"id": 1, "name": None}, {"id": 2, "name": "Engineering"}]
    posting = GreenhouseAdapter().normalize(job, "faire")
    assert posting.location_raw == ""
    assert posting.department == "Engineering"


async def test_greenhouse_unknown_board_raises_without_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404, json={"status": 404, "error": "Job not found"})

    async with make_client(handler) as client:
        with pytest.raises(SourceFetchError) as err:
            await GreenhouseAdapter().fetch(client, FakeCompany(slug="nope", board_token="nope"))
    assert err.value.status_code == 404
    assert len(calls) == 1


async def test_greenhouse_unexpected_shape_raises():
    async with make_client(lambda r: httpx.Response(200, json=[1, 2])) as client:
        with pytest.raises(SourceFetchError, match="'jobs' list"):
            await GreenhouseAdapter().fetch(client, FakeCompany(slug="faire", board_token="faire"))


def test_get_adapter_registry():
    adapter = get_adapter("greenhouse")
    assert isinstance(adapter, GreenhouseAdapter)
    assert isinstance(adapter, SourceAdapter)
    with pytest.raises(ValueError, match="unknown radar source"):
        get_adapter("workday")


# --- HTML to text ---------------------------------------------------------------------------------


def test_html_to_text_unescapes_and_keeps_structure():
    raw = (
        "&lt;h2&gt;About&amp;nbsp;us&lt;/h2&gt;&lt;p&gt;We build   things &amp;amp; ship.&lt;/p&gt;"
        "&lt;ul&gt;&lt;li&gt;&lt;p&gt;Python&lt;/p&gt;&lt;/li&gt;&lt;li&gt;Go&lt;br&gt;Rust&lt;/li&gt;&lt;/ul&gt;"
        "&lt;script&gt;alert(1)&lt;/script&gt;&lt;p&gt;Caf&amp;eacute; &amp;lt;3&lt;/p&gt;"
    )
    assert html_to_text(raw) == "About us\n\nWe build things & ship.\n\n- Python\n- Go\nRust\n\nCafé <3"
    assert html_to_text(None) == ""
    assert html_to_text("") == ""


# --- Polite client --------------------------------------------------------------------------------


async def test_user_agent_header_set():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async with make_client(handler) as client:
        assert await client.get_json("https://example.test/a", {"x": "1"}) == {"ok": True}
    assert seen[0].headers["User-Agent"] == USER_AGENT
    assert "OrbitInternshipRadar" in USER_AGENT
    assert seen[0].url.params["x"] == "1"

    # An injected AsyncClient still gets the User-Agent.
    injected = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fake = FakeTime()
    client = PoliteClient(injected, clock=fake.clock, sleep=fake.sleep)
    await client.get_json("https://example.test/b")
    await client.aclose()
    assert not injected.is_closed  # we don't close a client we didn't create
    await injected.aclose()
    assert seen[1].headers["User-Agent"] == USER_AGENT


async def test_retries_429_honoring_retry_after_then_succeeds():
    responses = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(503),
        httpx.Response(200, json={"jobs": []}),
    ]
    fake = FakeTime()
    async with make_client(lambda r: responses.pop(0), fake) as client:
        assert await client.get_json("https://example.test/jobs") == {"jobs": []}
    assert responses == []
    # 7s from Retry-After, then exponential backoff (base 1s, second retry -> 2s); spacing never adds more.
    assert fake.sleeps == [7.0, 2.0]


async def test_gives_up_after_max_retries_with_clear_error():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    fake = FakeTime()
    async with make_client(handler, fake) as client:
        with pytest.raises(SourceFetchError) as err:
            await client.get_json("https://example.test/jobs")
    assert len(calls) == 4  # 1 try + 3 retries
    assert err.value.attempts == 4 and err.value.status_code == 503
    assert "after 4 attempts" in str(err.value) and "HTTP 503" in str(err.value)
    assert fake.sleeps == [1.0, 2.0, 4.0]


async def test_transport_errors_are_retried():
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=[1])

    fake = FakeTime()
    async with make_client(handler, fake) as client:
        assert await client.get_json("https://example.test/") == [1]
    assert fake.sleeps == [1.0, 2.0]


async def test_retry_after_longer_than_max_backoff_gives_up():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "3600"})

    fake = FakeTime()
    async with make_client(handler, fake) as client:
        with pytest.raises(SourceFetchError, match="asked to wait 3600s"):
            await client.get_json("https://example.test/")
    assert len(calls) == 1 and fake.sleeps == []


def test_parse_retry_after():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after(format_datetime(now + timedelta(seconds=30), usegmt=True), now=now) == 30.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None


async def test_spacing_between_requests_to_same_host():
    fake = FakeTime()
    async with make_client(lambda r: httpx.Response(200, json={}), fake) as client:
        await client.get_json("https://a.example.test/1")
        await client.get_json("https://b.example.test/1")  # different host: no wait
        assert fake.sleeps == []
        fake.now += 0.25
        await client.get_json("https://a.example.test/2")  # same host, 0.25s later: waits the remaining 0.75s
        assert fake.sleeps == [pytest.approx(0.75)]
        fake.now += 5
        await client.get_json("https://a.example.test/3")  # long enough ago: no wait
        assert fake.sleeps == [pytest.approx(0.75)]


async def test_concurrent_same_host_requests_are_spaced():
    starts: list[float] = []
    fake = FakeTime()

    def handler(request):
        starts.append(fake.now)
        return httpx.Response(200, json={})

    async with make_client(handler, fake) as client:
        await asyncio.gather(*(client.get_json(f"https://a.example.test/{i}") for i in range(4)))
    assert len(starts) == 4
    assert fake.sleeps == [1.0, 1.0, 1.0]
    assert all(b - a >= 1.0 for a, b in zip(starts, starts[1:], strict=False))


async def test_global_concurrency_limit():
    in_flight = 0
    peak = 0

    async def handler(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        for _ in range(5):
            await asyncio.sleep(0)
        in_flight -= 1
        return httpx.Response(200, json={})

    async with make_client(handler) as client:
        await asyncio.gather(*(client.get_json(f"https://h{i}.example.test/") for i in range(10)))
    assert peak == 4


# --- NormalizedPosting ----------------------------------------------------------------------------


def _posting(**overrides) -> NormalizedPosting:
    fields = dict(
        source="greenhouse",
        external_id="1",
        company_slug="acme",
        title="Software Engineering Intern",
        url="https://example.test/1",
        location_raw="Toronto, ON",
        department="Engineering",
        description_text="Build things.",
        updated_at=datetime(2026, 1, 1, 9, 0),
        raw={"id": 1},
    )
    fields.update(overrides)
    return NormalizedPosting(**fields)


def test_content_hash_stable_and_sensitive_to_content():
    base = _posting()
    h = base.content_hash()
    assert len(h) == 64 and h == _posting().content_hash()
    # Not part of the hash: raw payload, timestamps, url, ids.
    assert _posting(raw={"id": 1, "extra": True}, updated_at=None, url="https://x.test/").content_hash() == h
    # Part of the hash.
    assert _posting(description_text="Build other things.").content_hash() != h
    assert _posting(title="Data Intern").content_hash() != h
    assert _posting(location_raw="Waterloo, ON").content_hash() != h
    assert _posting(department=None).content_hash() != h


def test_posting_defaults_and_naive_timestamps():
    p = NormalizedPosting(
        source="greenhouse",
        external_id="9",
        company_slug="acme",
        title="t",
        url="u",
        updated_at=datetime(2026, 1, 1, 12, 0, 0, 123, tzinfo=UTC),
    )
    assert p.location_raw == "" and p.description_text == "" and p.department is None and p.raw == {}
    assert p.updated_at.tzinfo is None and p.updated_at.microsecond == 0
