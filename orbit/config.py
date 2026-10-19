"""Settings (.env) + config.yaml loading."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_home() -> Path:
    return Path(os.environ.get("ORBIT_HOME") or Path(__file__).resolve().parent.parent)


class Settings(BaseSettings):
    """Secrets and machine-specific values, read from .env / environment."""

    model_config = SettingsConfigDict(
        env_file=str(default_home() / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_owner_chat_id: int | None = None
    proxy_url: str | None = None
    # Dev default matches docker-compose.yml; override with DATABASE_URL.
    database_url: str = "postgresql+asyncpg://orbit:orbit@127.0.0.1:5432/orbit"
    orbit_home: Path = Field(default_factory=default_home)
    # Bind-address overrides for the local API (env ORBIT_API_HOST / ORBIT_API_PORT), e.g. 0.0.0.0 in Docker.
    orbit_api_host: str | None = None
    orbit_api_port: int | None = None
    # Nightly plain-file snapshots (`orbit export`); relative paths are under ORBIT_HOME.
    export_dir: Path = Path("data/exports")

    @property
    def data_dir(self) -> Path:
        return self.orbit_home / "data"

    @property
    def logs_dir(self) -> Path:
        return self.orbit_home / "logs"

    @property
    def memory_dir(self) -> Path:
        return self.orbit_home / "memory"

    @property
    def persona_dir(self) -> Path:
        return self.orbit_home / "persona"

    @property
    def media_dir(self) -> Path:
        """Photos received over Telegram, one folder per day (data/media/YYYY-MM-DD/)."""
        return self.data_dir / "media"

    @property
    def exports_dir(self) -> Path:
        return self.export_dir if self.export_dir.is_absolute() else self.orbit_home / self.export_dir

    def packs_path(self, packs: PacksConfig) -> Path:
        path = Path(packs.dir)
        return path if path.is_absolute() else self.orbit_home / path

    def api_bind(self, api: ApiConfig) -> tuple[str, int]:
        """Host and port the local API listens on: env overrides first, then config.yaml."""
        return self.orbit_api_host or api.host, self.orbit_api_port or api.port

    def api_key_for(self, env_name: str) -> str | None:
        """Look up a provider key by env var name; OPENAI_API_KEY and ANTHROPIC_API_KEY also come from .env."""
        value = os.environ.get(env_name)
        if value:
            return value
        if env_name == "OPENAI_API_KEY":
            return self.openai_api_key
        if env_name == "ANTHROPIC_API_KEY":
            return self.anthropic_api_key
        return None


class ProviderConfig(BaseModel):
    # "openai": any OpenAI-compatible endpoint (base_url required). "anthropic": the Anthropic Messages API.
    type: Literal["openai", "anthropic"] = "openai"
    base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"


class TaskConfig(BaseModel):
    provider: str
    model: str
    params: dict[str, Any] = Field(default_factory=dict)


class AgentConfig(BaseModel):
    name: str = "Orbit"
    history_turns: int = 20
    max_tool_rounds: int = 8


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class TelegramConfig(BaseModel):
    enabled: bool = True


class BriefingJobConfig(BaseModel):
    enabled: bool = True
    cron: str = "0 8 * * *"


class ExportJobConfig(BaseModel):
    """Nightly `orbit export` snapshot + pg_dump into EXPORT_DIR, keeping the newest `keep` of each."""

    enabled: bool = True
    cron: str = "0 3 * * *"
    keep: int = Field(default=14, ge=1)


class JobsConfig(BaseModel):
    briefing: BriefingJobConfig = Field(default_factory=BriefingJobConfig)
    export: ExportJobConfig = Field(default_factory=ExportJobConfig)
    startup_notice: bool = True


class PacksConfig(BaseModel):
    """Domain packs (packs/<name>/pack.yaml + SKILL.md) loaded at startup."""

    dir: str = "packs"  # relative to ORBIT_HOME
    enabled: list[str] = Field(default_factory=list)


class RadarConfig(BaseModel):
    """Internship Radar preferences (config.yaml `radar:`); the watcher itself is enabled under `watchers:`."""

    # Terms in PostingExtraction's "<Season> <YYYY>" form. In October 2026 (3A fall term) Summer 2027 is
    # the main recruiting cycle and Fall 2027 postings start to appear; most Winter 2027 roles are filled.
    terms: list[str] = Field(default_factory=lambda: ["Summer 2027", "Fall 2027"])
    # Canonical extracted locations: "City, XX" (two-letter province) or "Remote (Canada)".
    locations: list[str] = Field(
        default_factory=lambda: ["Toronto, ON", "Waterloo, ON", "Kitchener, ON", "Remote (Canada)"]
    )
    # Hard filter on the extracted role type (orbit.radar.models.RoleType values, e.g. swe, ml_ai).
    # Empty means any role. Unknown names fail at load instead of silently matching nothing.
    role_types: list[str] = Field(default_factory=list)
    # Cosine-similarity cut-off for a Telegram push. Placeholder until calibrated in M5.5.
    notify_threshold: float = Field(default=0.45, ge=-1.0, le=1.0)
    # Relative to ORBIT_HOME; same default as orbit.radar.profile.DEFAULT_RESUME_PATH.
    resume_path: str = "data/resume.md"
    max_extract_per_poll: int = Field(default=40, ge=0)
    max_notifications_per_poll: int = Field(default=5, ge=0)

    @field_validator("role_types")
    @classmethod
    def _known_role_types(cls, values: list[str]) -> list[str]:
        from orbit.radar.models import RoleType  # local import: keep config free of radar imports at load

        out: list[str] = []
        for value in values:
            key = str(value).strip().lower()
            if key not in RoleType.__members__:
                allowed = ", ".join(RoleType.__members__)
                raise ValueError(f"unknown radar.role_types entry {value!r}; use one of: {allowed}")
            if key not in out:
                out.append(key)
        return out


class Config(BaseModel):
    providers: dict[str, ProviderConfig]
    tasks: dict[str, TaskConfig]
    agent: AgentConfig = Field(default_factory=AgentConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    jobs: JobsConfig = Field(default_factory=JobsConfig)
    watchers: list[dict[str, Any]] = Field(default_factory=list)
    radar: RadarConfig = Field(default_factory=RadarConfig)
    packs: PacksConfig = Field(default_factory=PacksConfig)


def load_config(path: Path | None = None) -> Config:
    path = path or default_home() / "config.yaml"
    with open(path, encoding="utf-8") as f:
        return Config.model_validate(yaml.safe_load(f) or {})
