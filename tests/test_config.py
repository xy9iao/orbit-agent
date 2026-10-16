"""ORBIT_API_HOST / ORBIT_API_PORT override the local API bind address from config.yaml (used in Docker)."""

from __future__ import annotations

import pytest

from orbit.config import ApiConfig, Settings


def make_settings() -> Settings:
    return Settings(_env_file=None)


def test_api_bind_defaults_to_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORBIT_API_HOST", raising=False)
    monkeypatch.delenv("ORBIT_API_PORT", raising=False)

    assert make_settings().api_bind(ApiConfig()) == ("127.0.0.1", 8765)
    assert make_settings().api_bind(ApiConfig(host="127.0.0.2", port=9000)) == ("127.0.0.2", 9000)


def test_api_bind_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORBIT_API_HOST", "0.0.0.0")
    monkeypatch.delenv("ORBIT_API_PORT", raising=False)
    assert make_settings().api_bind(ApiConfig()) == ("0.0.0.0", 8765)

    monkeypatch.setenv("ORBIT_API_PORT", "18765")
    assert make_settings().api_bind(ApiConfig()) == ("0.0.0.0", 18765)


def test_api_bind_rejects_bad_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORBIT_API_PORT", "not-a-port")
    with pytest.raises(ValueError):
        make_settings()
