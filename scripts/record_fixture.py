"""Dev-only: record a trimmed, real job-board API response as a test fixture.

This is the ONE place in the repo that touches the live job-board APIs on purpose. Tests replay the
fixture through httpx.MockTransport and never hit the network.

    python scripts/record_fixture.py greenhouse <board_token> [--limit 5] [--max-job-bytes 8000]

Writes tests/fixtures/radar/<source>_<token>.json with the response structure unchanged, at most
`--limit` jobs (internship/co-op titles first, so later extraction tests have something to chew on),
and each job's longest string fields shortened until the job fits in `--max-job-bytes`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orbit.radar.sources import available_sources, get_adapter  # noqa: E402
from orbit.radar.sources.http import PoliteClient, SourceFetchError  # noqa: E402

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "radar"
INTERN_RE = re.compile(r"\b(intern|internship|co-?op|student)\b", re.IGNORECASE)
MIN_KEEP = 300  # never shorten a string below this many characters


def job_list(payload: Any) -> tuple[list[Any], Any]:
    """Find the list of jobs in a board response: a top-level list (Lever) or a 'jobs' key (Greenhouse, Ashby)."""
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
        return payload["jobs"], "jobs"
    raise SystemExit(f"don't know where the jobs are in this response (keys: {list(payload)[:10]})")


def _title(job: Any) -> str:
    return str(job.get("title") or job.get("text") or "") if isinstance(job, dict) else ""


def _strings(node: Any, path: tuple = ()) -> list[tuple[tuple, str]]:
    if isinstance(node, str):
        return [(path, node)]
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in _strings(v, (*path, k))]
    if isinstance(node, list):
        return [s for i, v in enumerate(node) for s in _strings(v, (*path, i))]
    return []


def _set(node: Any, path: tuple, value: str) -> None:
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def _cut(text: str, length: int) -> str:
    """Shorten without splitting an HTML entity like '&lt;' (Greenhouse content is HTML-escaped)."""
    cut = text[:length]
    amp = cut.rfind("&")
    if amp != -1 and ";" not in cut[amp:] and length - amp < 10:
        cut = cut[:amp]
    return cut.rstrip() + " ..."


def shrink_job(job: Any, max_bytes: int) -> Any:
    """Shorten the longest strings in one job until its JSON is at most max_bytes (or nothing is left to cut)."""
    for _ in range(50):
        size = len(json.dumps(job, ensure_ascii=False).encode("utf-8"))
        if size <= max_bytes:
            break
        path, longest = max(_strings(job), key=lambda s: len(s[1]), default=((), ""))
        if not path or len(longest) <= MIN_KEEP:
            break
        target = max(MIN_KEEP, len(longest) - (size - max_bytes) - 16)
        _set(job, path, _cut(longest, target))
    return job


def trim(payload: Any, limit: int, max_job_bytes: int) -> Any:
    jobs, key = job_list(payload)
    ordered = sorted(jobs, key=lambda j: 0 if INTERN_RE.search(_title(j)) else 1)  # stable: keeps API order
    kept = [shrink_job(j, max_job_bytes) for j in ordered[:limit]]
    if key is None:
        return kept
    return {**payload, key: kept}


async def record(source: str, token: str, limit: int, max_job_bytes: int) -> Path:
    adapter = get_adapter(source)
    url, params = adapter.board_request(token)
    async with PoliteClient() as client:
        payload = await client.get_json(url, params)
    jobs, _ = job_list(payload)
    trimmed = trim(payload, limit, max_job_bytes)
    safe_token = re.sub(r"[^A-Za-z0-9_.-]+", "_", token)
    out = FIXTURE_DIR / f"{source}_{safe_token}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(trimmed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    kept, _ = job_list(trimmed)
    print(f"{source}/{token}: {len(jobs)} jobs on the board, kept {len(kept)} -> {out.relative_to(ROOT)}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", choices=available_sources())
    parser.add_argument("board_token")
    parser.add_argument("--limit", type=int, default=5, help="max jobs to keep (default 5)")
    parser.add_argument("--max-job-bytes", type=int, default=8000, help="max JSON bytes per job (default 8000)")
    args = parser.parse_args()
    try:
        asyncio.run(record(args.source, args.board_token, args.limit, args.max_job_bytes))
    except SourceFetchError as e:
        raise SystemExit(f"error: {e}") from None


if __name__ == "__main__":
    main()
