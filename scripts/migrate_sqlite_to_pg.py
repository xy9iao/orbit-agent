"""One-off copy of an old-schema SQLite Orbit database (data/orbit.db) into Postgres.

Copies messages, reminders, usage and kv. Safe to re-run: original ids are kept,
rows that already exist are skipped (ON CONFLICT DO NOTHING), and each id sequence
is moved past the largest copied id so new rows never collide.

    python scripts/migrate_sqlite_to_pg.py                       # data/orbit.db -> DATABASE_URL
    python scripts/migrate_sqlite_to_pg.py path/to/orbit.db --url postgresql+asyncpg://...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Table, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import create_async_engine

from orbit.config import Settings
from orbit.storage.migrate import upgrade_head
from orbit.storage.models import KV, Message, Reminder, Usage

TABLES: list[tuple[Table, list[str]]] = [  # (target table, timestamp columns)
    (Message.__table__, ["created_at"]),
    (Reminder.__table__, ["due_at", "created_at"]),
    (Usage.__table__, ["created_at"]),
    (KV.__table__, []),
]
CHUNK = 1000  # rows per INSERT, well under asyncpg's 32767 bind-parameter limit


def _timestamp(value: str) -> datetime:
    """Old rows are naive local ISO strings; the columns are naive local timestamps."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def read_sqlite(path: Path) -> dict[str, list[dict[str, Any]]]:
    """All rows of the four tables, converted to Postgres column types. A missing table reads as empty."""
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    data: dict[str, list[dict[str, Any]]] = {}
    try:
        for table, ts_cols in TABLES:
            found = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table.name,))
            if not found.fetchone():
                data[table.name] = []
                continue
            cols = ", ".join(c.name for c in table.columns)
            rows = [dict(r) for r in conn.execute(f'SELECT {cols} FROM "{table.name}"')]
            for row in rows:
                for col in ts_cols:
                    row[col] = _timestamp(row[col])
                if table.name == "kv":
                    row["value"] = json.loads(row["value"])
            data[table.name] = rows
    finally:
        conn.close()
    return data


async def migrate_sqlite(sqlite_path: Path, url: str) -> dict[str, tuple[int, int]]:
    """Copy the four core tables; returns {table: (copied, skipped)}."""
    if not sqlite_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {sqlite_path}")
    data = read_sqlite(sqlite_path)
    engine = create_async_engine(url)
    counts: dict[str, tuple[int, int]] = {}
    try:
        await upgrade_head(engine)
        async with engine.begin() as conn:  # one transaction: all tables or nothing
            for table, _ in TABLES:
                rows = data[table.name]
                pk = table.primary_key.columns.values()[0]
                copied = 0
                for i in range(0, len(rows), CHUNK):
                    stmt = (
                        pg_insert(table)
                        .values(rows[i : i + CHUNK])
                        .on_conflict_do_nothing(index_elements=[pk])
                        .returning(pk)
                    )
                    copied += len((await conn.execute(stmt)).all())
                counts[table.name] = (copied, len(rows) - copied)
                if pk.name == "id":
                    # Explicit ids bypass the sequence; move it past max(id) (or reset it on an empty table).
                    await conn.execute(
                        text(
                            f"SELECT setval(pg_get_serial_sequence('\"{table.name}\"', 'id'), "
                            f'COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM "{table.name}"'
                        )
                    )
    finally:
        await engine.dispose()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sqlite_path", nargs="?", type=Path, help="default: <ORBIT_HOME>/data/orbit.db")
    parser.add_argument("--url", help="target database URL (default: DATABASE_URL or Settings().database_url)")
    args = parser.parse_args()

    settings = Settings()
    sqlite_path = args.sqlite_path or settings.data_dir / "orbit.db"
    url = args.url or os.environ.get("DATABASE_URL") or settings.database_url

    counts = asyncio.run(migrate_sqlite(sqlite_path, url))
    for name, (copied, skipped) in counts.items():
        print(f"{name:<10} copied {copied:>6}  skipped {skipped:>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
