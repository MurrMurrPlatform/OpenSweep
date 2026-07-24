"""One `codex app-server` process per subscription (keyed by uid+credential
revision), lazily spawned and reused across concurrent runs. The single process
owns the auth.json and its token refresh — so concurrent threads share ONE
credential with no rotation race (spike-verified). Seeds a worker-private
CODEX_HOME from the sealed secret ONCE (never per run — per OpenAI's rule)."""
from __future__ import annotations

import asyncio

from domains.llm_providers.services.codex_app_server import AppServerClient
from domains.llm_providers.services.runtime_env import apply_runtime_to_env, build_runtime
from domains.executors.agent_env import build_agent_env
from logging_config import logger


def _seed_codex_home(provider) -> dict:
    """Write the private CODEX_HOME/auth.json from the sealed secret and return
    the env (+cwd) the app-server should run with. Reuses the same seeding the
    exec path uses (build_runtime + apply_runtime_to_env)."""
    runtime = build_runtime(provider)
    env = build_agent_env(run_uid="", extra=runtime.env_vars)
    env = apply_runtime_to_env(runtime, env)
    return {"env": env, "cwd": None}


def _key(provider) -> tuple[str, int]:
    uid = (provider.uid or "").strip()
    if not uid:
        raise ValueError("codex app-server registry: provider.uid is required")
    return (uid, int(getattr(provider, "credential_revision", 0) or 0))


class AppServerRegistry:
    def __init__(self, spawn=AppServerClient.spawn):
        self._spawn = spawn
        self._clients: dict[tuple[str, int], AppServerClient] = {}
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}

    def _lock(self, key) -> asyncio.Lock:
        self._locks.setdefault(key, asyncio.Lock())
        return self._locks[key]

    async def acquire(self, provider) -> AppServerClient:
        key = _key(provider)
        async with self._lock(key):
            existing = self._clients.get(key)
            if existing is not None:
                return existing
            seeded = _seed_codex_home(provider)
            client = await self._spawn(
                argv=["codex", "app-server", "--stdio"], env=seeded["env"], cwd=seeded["cwd"],
            )
            await client.initialize()
            self._clients[key] = client
            logger.info(f"codex app-server started for subscription {key[0]} rev {key[1]}",
                        extra={"tag": "codex"})
            return client

    async def shutdown(self, provider) -> None:
        key = _key(provider)
        async with self._lock(key):
            client = self._clients.pop(key, None)
        if client is not None:
            await client.close()

    async def shutdown_all(self) -> None:
        for client in list(self._clients.values()):
            await client.close()
        self._clients.clear()


REGISTRY = AppServerRegistry()
