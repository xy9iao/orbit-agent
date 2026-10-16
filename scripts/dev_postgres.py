"""Start a local Postgres + pgvector for development when Docker is not available.

Prefer `docker compose up -d postgres`. This fallback uses the dev-only `pgserver`
package (bundled Postgres binaries with pgvector), installed with `pip install pgserver`.
The server keeps running after this script exits; data lives in data/pgserver (git-ignored).

    python scripts/dev_postgres.py          # start (or reuse) and create orbit + orbit_test databases
    python scripts/dev_postgres.py --stop
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PGDATA = ROOT / "data" / "pgserver"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()

    import pgserver  # dev-only dependency: pip install pgserver

    if args.stop:
        from pgserver._commands import pg_ctl

        pg_ctl(["-D", str(PGDATA), "stop"])
        return 0

    PGDATA.parent.mkdir(parents=True, exist_ok=True)
    srv = pgserver.get_server(PGDATA, cleanup_mode=None)
    for db in ("orbit", "orbit_test"):
        exists = srv.psql(f"SELECT 1 FROM pg_database WHERE datname='{db}';")
        if "1 row" not in exists:
            srv.psql(f"CREATE DATABASE {db};")
    # The vector extension itself is created by the 0001_core migration.
    print(srv.get_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
