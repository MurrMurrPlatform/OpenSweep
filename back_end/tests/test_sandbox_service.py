"""Sandbox lifecycle hardening: destroy-vs-live-run race, cleanup_expired's
TOCTOU window, and the subprocess timeout on _run(). DB-free — Sandbox/Run
are faked so these exercise pure service logic.
"""

from datetime import UTC, datetime, timedelta

import pytest

import domains.execution.services.sandbox_service as svc
import domains.runs.services.turn_service as turn_svc
from domains.execution.schemas import SandboxStatus

pytestmark = pytest.mark.asyncio


async def _anoop(*a, **k):
    return None


class _Sandbox:
    def __init__(self, uid, *, status=SandboxStatus.READY.value, cleanup_after=None):
        self.uid = uid
        self.repository_uid = "repo1"
        self.host_path = "/host/x"
        self.container_path = f"/host/sandboxes/{uid}"
        self.source_branch = "main"
        self.sandbox_branch = "opensweep/work"
        self.purpose = "write"
        self.status = status
        self.created_at = datetime.now(UTC)
        self.destroyed_at = None
        self.cleanup_after = cleanup_after
        self.error = ""

    async def save(self):
        return None


class _FakeNodeSet(list):
    """Chainable stand-in for neomodel's AsyncBaseSet: .filter()/.exclude()
    narrow synchronously, the whole thing is awaited at the end."""

    def filter(self, **kw):
        out = list(self)
        for k, v in kw.items():
            if k.endswith("__lte"):
                field = k[: -len("__lte")]
                out = [i for i in out if getattr(i, field) is not None and getattr(i, field) <= v]
            else:
                out = [i for i in out if getattr(i, k) == v]
        return _FakeNodeSet(out)

    def exclude(self, **kw):
        out = list(self)
        for k, v in kw.items():
            out = [i for i in out if getattr(i, k) != v]
        return _FakeNodeSet(out)

    def __await__(self):
        async def _coro():
            return list(self)

        return _coro().__await__()


def _fake_sandbox_nodes(store: dict, *, forbid_all=False):
    class _Nodes:
        @staticmethod
        async def get_or_none(uid):
            return store.get(uid)

        @staticmethod
        def filter(**kw):
            return _FakeNodeSet(store.values()).filter(**kw)

        @staticmethod
        async def all():
            if forbid_all:
                raise AssertionError("cleanup_expired must not do a full Sandbox.nodes.all() scan")
            return list(store.values())

    return _Nodes


class _Run:
    def __init__(self, uid, sandbox_uid):
        self.uid = uid
        self.sandbox_uid = sandbox_uid


def _fake_run_nodes(runs: list):
    class _Nodes:
        @staticmethod
        async def filter(**kw):
            sandbox_uid = kw.get("sandbox_uid")
            return [r for r in runs if r.sandbox_uid == sandbox_uid]

    return _Nodes


# ── destroy() must stop a live run's turn before rmtree ─────────────────────


async def test_destroy_kills_live_run_before_rmtree(monkeypatch):
    sb = _Sandbox("sb1")
    monkeypatch.setattr(svc.Sandbox, "nodes", _fake_sandbox_nodes({"sb1": sb}))
    monkeypatch.setattr(svc, "write_audit", _anoop)

    run = _Run("run1", "sb1")
    import domains.runs.models as run_models

    monkeypatch.setattr(run_models.Run, "nodes", _fake_run_nodes([run]))

    calls = []

    async def _fake_kill(uid):
        calls.append(("kill", uid))

    monkeypatch.setattr(turn_svc, "kill_running_turn", _fake_kill)

    def _fake_rmtree(path, ignore_errors=False):
        calls.append(("rmtree", path))

    monkeypatch.setattr(svc.shutil, "rmtree", _fake_rmtree)

    await svc.SandboxService().destroy("sb1")

    assert calls == [("kill", "run1"), ("rmtree", "/host/sandboxes/sb1")]
    assert sb.status == SandboxStatus.DESTROYED.value


async def test_destroy_is_a_noop_when_no_run_references_the_sandbox(monkeypatch):
    sb = _Sandbox("sb1")
    monkeypatch.setattr(svc.Sandbox, "nodes", _fake_sandbox_nodes({"sb1": sb}))
    monkeypatch.setattr(svc, "write_audit", _anoop)

    import domains.runs.models as run_models

    monkeypatch.setattr(run_models.Run, "nodes", _fake_run_nodes([]))
    monkeypatch.setattr(svc.shutil, "rmtree", lambda *a, **k: None)

    result = await svc.SandboxService().destroy("sb1")
    assert result.status == SandboxStatus.DESTROYED.value


# ── cleanup_expired(): server-side filter + TOCTOU re-check ─────────────────


async def test_cleanup_expired_filters_serverside_not_a_full_scan(monkeypatch):
    now = datetime.now(UTC)
    expired = _Sandbox("expired", cleanup_after=now - timedelta(hours=1))
    fresh = _Sandbox("fresh", cleanup_after=now + timedelta(hours=1))
    store = {"expired": expired, "fresh": fresh}
    monkeypatch.setattr(svc.Sandbox, "nodes", _fake_sandbox_nodes(store, forbid_all=True))
    monkeypatch.setattr(svc, "write_audit", _anoop)

    import domains.runs.models as run_models

    monkeypatch.setattr(run_models.Run, "nodes", _fake_run_nodes([]))
    monkeypatch.setattr(svc.shutil, "rmtree", lambda *a, **k: None)

    count = await svc.SandboxService().cleanup_expired()

    assert count == 1
    assert expired.status == SandboxStatus.DESTROYED.value
    assert fresh.status == SandboxStatus.READY.value


async def test_cleanup_expired_respects_a_touch_that_lands_before_destroy(monkeypatch):
    """The TOCTOU case: the sandbox was expired when cleanup_expired scanned
    it, but a concurrent touch() slid cleanup_after into the future before
    the loop got around to destroying it. The fresh re-read must win."""
    now = datetime.now(UTC)
    sb = _Sandbox("sb1", cleanup_after=now - timedelta(seconds=1))
    store = {"sb1": sb}
    monkeypatch.setattr(svc.Sandbox, "nodes", _fake_sandbox_nodes(store))
    monkeypatch.setattr(svc, "write_audit", _anoop)

    import domains.runs.models as run_models

    monkeypatch.setattr(run_models.Run, "nodes", _fake_run_nodes([]))
    destroy_calls = []
    monkeypatch.setattr(svc.shutil, "rmtree", lambda *a, **k: destroy_calls.append(a))

    # Simulate touch() racing in between the scan and the destroy call: by
    # the time cleanup_expired re-reads it, retention has been extended.
    sb.cleanup_after = now + timedelta(hours=1)

    count = await svc.SandboxService().cleanup_expired()

    assert count == 0
    assert sb.status == SandboxStatus.READY.value
    assert destroy_calls == []


# ── _run(): subprocess timeout ───────────────────────────────────────────────


async def test_run_times_out_and_kills_a_hanging_subprocess():
    with pytest.raises(RuntimeError, match="timed out after"):
        await svc._run(["sleep", "5"], timeout=0.2)


async def test_run_succeeds_within_timeout():
    await svc._run(["true"], timeout=5)  # no raise


# ── turn_service.kill_running_turn ──────────────────────────────────────────


async def test_kill_running_turn_is_a_noop_when_nothing_is_in_flight(monkeypatch):
    monkeypatch.setattr(turn_svc, "_RUNNING", {})
    await turn_svc.kill_running_turn("no-such-run")  # no raise


async def test_kill_running_turn_kills_the_tracked_process(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(turn_svc, "_RUNNING", {"run1": sentinel})
    monkeypatch.setattr(turn_svc, "_INTERRUPTED", set())

    calls = []

    async def _fake_kill(proc):
        calls.append(proc)

    monkeypatch.setattr(turn_svc, "_kill_turn_process", _fake_kill)

    await turn_svc.kill_running_turn("run1")

    assert calls == [sentinel]
    assert "run1" in turn_svc._INTERRUPTED
