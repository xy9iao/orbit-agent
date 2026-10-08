"""Settings (.env) + config.yaml loading."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_home() -> Path:
    return Path(os.environ.get("ORBIT_HOME") or Path(__file__).resolve().parent.parent)


class Settings(BaseSettings):
    """Secrets and machine-specific values, read from .env / environment."""

    model_config = SettingsConfigDict(
        env_file=str(default_home() / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    openai_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_owner_chat_id: int | None = None
    proxy_url: str | None = None
    orbit_home: Path = Field(default_factory=default_home)

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

    def api_key_for(self, env_name: str) -> str | None:
        """Look up a provider key by env var name; OPENAI_API_KEY also comes from .env."""
        value = os.environ.get(env_name)
        if value:
            return value
        if env_name == "OPENAI_API_KEY":
            return self.openai_api_key
        return None


class ProviderConfig(BaseModel):
    base_url: str
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


class JobsConfig(BaseModel):
    briefing: BriefingJobConfig = Field(default_factory=BriefingJobConfig)
    startup_notice: bool = True


class Config(BaseModel):
    providers: dict[str, ProviderConfig]
    tasks: dict[str, TaskConfig]
    agent: AgentConfig = Field(default_factory=AgentConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    jobs: JobsConfig = Field(default_factory=JobsConfig)
    watchers: list[dict[str, Any]] = Field(default_factory=list)


def load_config(path: Path | None = None) -> Config:
    path = path or default_home() / "config.yaml"
    with open(path, "r", encoding="utf-8") as f:
        return Config.model_validate(yaml.safe_load(f) or {})
