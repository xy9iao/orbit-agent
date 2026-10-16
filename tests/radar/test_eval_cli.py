"""`orbit radar eval` (M4.4 CLI): dry run on the real label file, a scored run on the small fixture."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from typer.testing import CliRunner

import orbit.radar.cli as radar_cli
from orbit.cli.main import app
from orbit.config import Settings
from orbit.core.router import ModelRouter
from orbit.radar.eval import KV_LAST_EVAL
from orbit.storage.db import Database
from tests.conftest import make_config
from tests.radar.test_eval import RaisingProvider, make_router, scripted_router

ROOT = Path(__file__).resolve().parents[2]
SMALL = ROOT / "tests" / "fixtures" / "radar" / "labels_small.yaml"
REAL = ROOT / "evals" / "radar_labels.yaml"
runner = CliRunner()


def _patch_runtime(monkeypatch, database_url: str, tmp_path: Path, router: ModelRouter) -> None:
    @asynccontextmanager
    async def fake_runtime(settings=None):
        database = Database(database_url)
        await database.connect()
        try:
            yield radar_cli.RadarRuntime(Settings(orbit_home=tmp_path), make_config(), database, router)
        finally:
            await database.close()

    monkeypatch.setattr(radar_cli, "open_runtime", fake_runtime)


async def _kv(database_url: str, key: str):
    database = Database(database_url)
    await database.connect()
    try:
        return await database.kv_get(key)
    finally:
        await database.close()


def test_eval_dry_run_on_real_labels_needs_no_db_or_model(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("dry run must not open the runtime")

    monkeypatch.setattr(radar_cli, "open_runtime", _boom)
    result = runner.invoke(app, ["radar", "eval", "--dry-run", "--labels", str(REAL)])
    assert result.exit_code == 0, result.output
    assert "30" in result.output
    assert "UNVERIFIED" in result.output


def test_eval_scores_and_saves_summary(monkeypatch, tmp_path, database_url):
    router, _ = scripted_router()
    _patch_runtime(monkeypatch, database_url, tmp_path, router)
    result = runner.invoke(app, ["radar", "eval", "--labels", str(SMALL)])
    assert result.exit_code == 0, result.output
    assert "is_internship" in result.output and "UNVERIFIED" in result.output
    saved = asyncio.run(_kv(database_url, KV_LAST_EVAL))
    assert saved["n"] == 3 and saved["unverified"] is True
    assert set(saved["fields"]) >= {"is_internship", "terms", "locations", "tech_stack"}


def test_eval_without_provider_exits_1(monkeypatch, tmp_path, database_url):
    _patch_runtime(monkeypatch, database_url, tmp_path, ModelRouter(make_config(), {}))
    result = runner.invoke(app, ["radar", "eval", "--labels", str(SMALL)])
    assert result.exit_code == 1
    assert "extract" in result.output


def test_eval_aborts_on_provider_outage_without_saving(monkeypatch, tmp_path, database_url):
    _patch_runtime(monkeypatch, database_url, tmp_path, make_router(RaisingProvider("cheap")))
    result = runner.invoke(app, ["radar", "eval", "--labels", str(SMALL)])
    assert result.exit_code == 1
    assert "no score saved" in result.output


def test_eval_rejects_bad_label_file(tmp_path):
    bad = tmp_path / "labels.yaml"
    bad.write_text("status: x\nitems: [{id: 1}]\n", encoding="utf-8")
    result = runner.invoke(app, ["radar", "eval", "--labels", str(bad)])
    assert result.exit_code == 1


def test_printable_replaces_unencodable(monkeypatch):
    import io
    import sys

    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="ascii"))
    assert radar_cli._printable("⚠ ok") == "? ok"
