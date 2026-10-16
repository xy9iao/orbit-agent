"""ModelRouter: code asks for a *task*, config decides provider + model."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from orbit.config import Config, Settings
from orbit.core.providers import OpenAIProvider, Provider, Response

UsageSink = Callable[[str, str, str, int, int], Awaitable[None]]


class ModelRouter:
    def __init__(
        self,
        config: Config,
        providers: dict[str, Provider],
        usage_sink: UsageSink | None = None,
    ):
        self.config = config
        self.providers = providers
        self.usage_sink = usage_sink

    @classmethod
    def from_config(
        cls, config: Config, settings: Settings, usage_sink: UsageSink | None = None
    ) -> ModelRouter:
        providers: dict[str, Provider] = {}
        for name, pc in config.providers.items():
            key = settings.api_key_for(pc.api_key_env)
            if not key:
                logger.warning("provider '{}' has no API key in env {} - skipped", name, pc.api_key_env)
                continue
            providers[name] = OpenAIProvider(
                name=name, base_url=pc.base_url, api_key=key, proxy=settings.proxy_url
            )
        return cls(config, providers, usage_sink)

    def resolve(self, task: str) -> tuple[Provider, str, dict[str, Any]]:
        tc = self.config.tasks.get(task) or self.config.tasks.get("chat")
        if tc is None:
            raise KeyError(f"no task config for '{task}' and no 'chat' fallback")
        provider = self.providers.get(tc.provider)
        if provider is None:
            raise RuntimeError(
                f"provider '{tc.provider}' for task '{task}' is not configured (missing API key?)"
            )
        return provider, tc.model, dict(tc.params)

    async def complete(
        self,
        task: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **overrides: Any,
    ) -> Response:
        provider, model, params = self.resolve(task)
        params.update(overrides)
        logger.debug("router: task={} -> {}:{}", task, provider.name, model)
        resp = await provider.complete(model, messages, tools, **params)
        if self.usage_sink and resp.usage:
            await self.usage_sink(
                task,
                provider.name,
                model,
                resp.usage.get("prompt_tokens", 0),
                resp.usage.get("completion_tokens", 0),
            )
        return resp

    async def embed(self, task: str, texts: list[str], **overrides: Any) -> list[list[float]]:
        """Embed `texts` with the task's model; returns one vector per text, in input order."""
        if not texts:
            return []
        provider, model, params = self.resolve(task)
        params.update(overrides)
        logger.debug("router: embed task={} -> {}:{} ({} texts)", task, provider.name, model, len(texts))
        resp = await provider.embed(model, texts, **params)
        if len(resp.vectors) != len(texts):
            raise RuntimeError(
                f"provider '{provider.name}' returned {len(resp.vectors)} vectors for {len(texts)} texts"
            )
        if self.usage_sink and resp.usage:
            await self.usage_sink(task, provider.name, model, resp.usage.get("prompt_tokens", 0), 0)
        return resp.vectors
