# tests/test_codex_app_server_registry.py
import pytest
from types import SimpleNamespace
from domains.llm_providers.services.codex_app_server_registry import AppServerRegistry
pytestmark = pytest.mark.asyncio


class _FakeClient:
    def __init__(self): self.initialized = 0; self.closed = False
    async def initialize(self): self.initialized += 1
    async def close(self): self.closed = True


def _provider(uid="p1", rev=0, secret="sealed-x"):
    return SimpleNamespace(uid=uid, kind="codex_subscription",
                           credential_secret=secret, credential_revision=rev)


async def test_acquire_reuses_one_server_per_subscription(monkeypatch):
    spawned = []
    async def fake_spawn(*, argv, env, cwd=None):
        c = _FakeClient(); spawned.append(c); return c
    # avoid real file writes when seeding
    import domains.llm_providers.services.codex_app_server_registry as reg
    monkeypatch.setattr(reg, "_seed_codex_home", lambda provider: {"env": {}, "cwd": None})

    r = AppServerRegistry(spawn=fake_spawn)
    p = _provider()
    a = await r.acquire(p)
    b = await r.acquire(p)          # same (uid, rev) → reuse
    assert a is b and len(spawned) == 1 and a.initialized == 1

    c = await r.acquire(_provider(rev=1))   # credential rotated → new server
    assert c is not a and len(spawned) == 2
    await r.shutdown_all()
    assert a.closed and c.closed


def test_codex_home_is_revision_scoped():
    from types import SimpleNamespace
    from domains.llm_providers.services.runtime_env import _codex_home
    p0 = SimpleNamespace(uid="p1", credential_revision=0)
    p1 = SimpleNamespace(uid="p1", credential_revision=1)
    assert _codex_home(p0) != _codex_home(p1)
    assert _codex_home(p0).endswith("opensweep-codex-p1-r0")
    assert _codex_home(p1).endswith("opensweep-codex-p1-r1")
