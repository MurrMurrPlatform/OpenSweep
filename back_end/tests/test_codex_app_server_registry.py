# tests/test_codex_app_server_registry.py
"""Phase 4b: the app-server session owns the credential lease for its lifetime.

These tests pin the property that makes `OPENSWEEP_CODEX_APP_SERVER=1` safe
under the prefork worker: one credential holder at a time (the lease is entered
once per server, not once per run), many concurrent runs inside it, and the
lease released on idle/shutdown so another process can take over.
"""
import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import domains.llm_providers.services.codex_app_server_registry as reg
from domains.llm_providers.services.codex_app_server_registry import AppServerRegistry

pytestmark = pytest.mark.asyncio


class _FakeClient:
    def __init__(self):
        self.initialized = 0
        self.closed = False
        self.alive = True

    async def initialize(self):
        self.initialized += 1

    async def close(self):
        self.closed = True
        self.alive = False


def _provider(uid="p1", rev=0, secret="sealed-x"):
    return SimpleNamespace(uid=uid, kind="codex_subscription",
                           credential_secret=secret, credential_revision=rev)


@pytest.fixture
def harness(monkeypatch):
    """Registry with the lease + seeding faked out, plus a record of lease
    enter/exit so tests can assert who holds the credential and when."""
    spawned: list[_FakeClient] = []
    lease: dict = {"enters": 0, "exits": 0, "fail": None, "revision": 0}

    async def fake_spawn(*, argv, env, cwd=None):
        c = _FakeClient()
        spawned.append(c)
        return c

    @contextlib.asynccontextmanager
    async def fake_txn(provider):
        if lease["fail"] is not None:
            raise lease["fail"]
        lease["enters"] += 1
        provider.credential_revision = lease["revision"]
        try:
            yield
        finally:
            lease["exits"] += 1

    async def fake_read_credential(uid):
        return ("sealed-x", lease["revision"])

    monkeypatch.setattr(reg.codex_credential, "codex_credential_txn", fake_txn)
    monkeypatch.setattr(reg.codex_credential, "_read_credential", fake_read_credential)
    monkeypatch.setattr(reg, "_seed_env", lambda provider: {})
    monkeypatch.setattr(reg, "_idle_shutdown_seconds", lambda: 0.05)

    return SimpleNamespace(
        registry=AppServerRegistry(spawn=fake_spawn), spawned=spawned, lease=lease,
    )


async def test_concurrent_runs_share_one_server_and_one_lease(harness):
    """The whole point of 4b: N runs on one subscription → 1 server, 1 lease."""
    p = _provider()
    a = await harness.registry.acquire(p)
    b = await harness.registry.acquire(p)

    assert a is b
    assert len(harness.spawned) == 1
    assert a.client.initialized == 1
    assert a.refs == 2
    # Entered ONCE for the server, not once per run — that is what lets the two
    # runs proceed concurrently instead of serializing on the lease.
    assert harness.lease["enters"] == 1
    assert harness.lease["exits"] == 0


async def test_lease_released_only_after_last_run_goes_idle(harness):
    p = _provider()
    a = await harness.registry.acquire(p)
    await harness.registry.acquire(p)

    await harness.registry.release(a)
    await asyncio.sleep(0.12)
    # One run still in flight → still held.
    assert harness.lease["exits"] == 0
    assert not a.client.closed

    await harness.registry.release(a)
    await asyncio.sleep(0.12)
    # Idle → server stopped, THEN lease released (write-back sees the final file).
    assert a.client.closed
    assert harness.lease["exits"] == 1


async def test_new_run_during_idle_countdown_keeps_the_server(harness):
    p = _provider()
    a = await harness.registry.acquire(p)
    await harness.registry.release(a)
    b = await harness.registry.acquire(p)   # arrives before the idle timer fires
    await asyncio.sleep(0.12)

    assert b is a
    assert not a.client.closed
    assert len(harness.spawned) == 1
    assert harness.lease["exits"] == 0


async def test_rotated_credential_recycles_the_server(harness):
    p = _provider()
    a = await harness.registry.acquire(p)
    await harness.registry.release(a)

    harness.lease["revision"] = 7          # user re-pasted the credential
    b = await harness.registry.acquire(_provider(rev=7))

    assert b is not a
    assert a.client.closed
    assert len(harness.spawned) == 2
    assert b.revision == 7


async def test_rotated_credential_does_not_kill_in_flight_runs(harness):
    """Recycling on a revision bump must wait for the server to go idle —
    tearing it down mid-turn would kill other runs sharing it."""
    p = _provider()
    a = await harness.registry.acquire(p)   # one run still in flight
    harness.lease["revision"] = 7

    b = await harness.registry.acquire(p)

    assert b is a
    assert not a.client.closed
    assert len(harness.spawned) == 1


async def test_dead_server_is_recycled(harness):
    p = _provider()
    a = await harness.registry.acquire(p)
    await harness.registry.release(a)
    a.client.alive = False                 # server crashed

    b = await harness.registry.acquire(p)

    assert b is not a
    assert len(harness.spawned) == 2


async def test_lease_contention_propagates_for_retry(harness):
    """Another process holds the subscription — callers turn this into a
    retryable PAUSED_QUOTA rather than a hard failure."""
    harness.lease["fail"] = HTTPException(status_code=503, detail="busy")

    with pytest.raises(HTTPException):
        await harness.registry.acquire(_provider())
    assert len(harness.spawned) == 0


async def test_failed_spawn_does_not_strand_the_lease(harness, monkeypatch):
    async def boom(*, argv, env, cwd=None):
        raise RuntimeError("codex not installed")

    r = AppServerRegistry(spawn=boom)
    with pytest.raises(RuntimeError):
        await r.acquire(_provider())
    # Entered then exited — the subscription is free for the next process.
    assert harness.lease["enters"] == 1
    assert harness.lease["exits"] == 1


async def test_shutdown_all_releases_every_subscription(harness):
    a = await harness.registry.acquire(_provider(uid="p1"))
    b = await harness.registry.acquire(_provider(uid="p2"))

    await harness.registry.shutdown_all()

    assert a.client.closed and b.client.closed
    assert harness.lease["exits"] == 2
    assert harness.registry._sessions == {}


def test_codex_home_is_revision_scoped():
    from domains.llm_providers.services.runtime_env import _codex_home
    p0 = SimpleNamespace(uid="p1", credential_revision=0)
    p1 = SimpleNamespace(uid="p1", credential_revision=1)
    assert _codex_home(p0) != _codex_home(p1)
    assert _codex_home(p0).endswith("opensweep-codex-p1-r0")
    assert _codex_home(p1).endswith("opensweep-codex-p1-r1")


async def test_idle_close_actually_releases_the_lease(harness):
    """`_close` cancels `session.idle_task` — which, on the idle path, IS the
    running task. Self-cancelling there raised CancelledError at the first await
    inside `_close` and skipped the lease teardown entirely, stranding the
    subscription until its TTL. Needs a client whose close() really suspends."""
    class _SuspendingClient(_FakeClient):
        async def close(self):
            await asyncio.sleep(0)      # a real suspension point
            await super().close()

    async def spawn(*, argv, env, cwd=None):
        c = _SuspendingClient()
        harness.spawned.append(c)
        return c

    harness.registry._spawn = spawn
    session = await harness.registry.acquire(_provider())
    await harness.registry.release(session)
    await asyncio.sleep(0.15)

    assert session.client.closed
    assert harness.lease["exits"] == 1, "lease stranded — teardown never ran"


async def test_session_from_a_dead_event_loop_is_discarded(harness):
    """A Celery run gets its own `asyncio.run` loop. A session cached in this
    process-level registry would outlive it, and the loop teardown force-releases
    the parked lease while the codex process is still alive. Such a session must
    never be handed to a new run."""
    session = await harness.registry.acquire(_provider())
    await harness.registry.release(session)
    session.loop = None                  # stand-in for "built on another loop"

    fresh = await harness.registry.acquire(_provider())

    assert fresh is not session
    assert len(harness.spawned) == 2


async def test_acquire_is_refused_while_shutting_down(harness):
    """A session registered after `shutdown_all` swept past it would hold the
    lease with nothing left to release it."""
    harness.registry._closing = True

    with pytest.raises(RuntimeError):
        await harness.registry.acquire(_provider())

    assert harness.registry._sessions == {}
    assert harness.lease["enters"] == harness.lease["exits"]
