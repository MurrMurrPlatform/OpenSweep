"""A run cancelled mid-clone must not leak its fresh sandbox.

`_prepare_dispatch_and_finalize` clones the workspace AFTER trigger_run has
returned the queued row, so the run can be cancelled (or reconciled stale)
while the clone is still running. The bail-out branch used to destroy the
clone only when `sandbox_factory is None` — but write runs (fix/implement)
ALWAYS arrive with a `sandbox_factory`, so exactly the sandboxes that branch
was meant to clean up were the ones it skipped: the Sandbox node survived,
`Run.sandbox_uid` stayed empty, and the clone held disk until the 30-minute
cleanup beat expired it.
"""

import pytest

from domains.runs.schemas import Executor, ExecutionMode, RunStatus, RunTrigger
from domains.runs.services import lifecycle

pytestmark = pytest.mark.asyncio


class _Sandbox:
    def __init__(self, uid):
        self.uid = uid
        self.container_path = f"/tmp/{uid}"


class _Run:
    def __init__(self, status):
        self.uid = "run-cancelled"
        self.status = status
        self.usage = {}
        self.sandbox_uid = ""
        self.executor = Executor.CLAUDE_CODE.value
        self.updated_at = None
        self.saves = 0

    async def save(self):
        self.saves += 1


def _harness(monkeypatch, run):
    """Stub everything around the bail-out branch; return the destroy log."""
    destroyed: list[str] = []

    class _RunNodes:
        async def get_or_none(self, uid):
            return run

    class _SandboxService:
        async def destroy(self, sandbox_uid, *, actor_uid=None):
            destroyed.append(sandbox_uid)

    monkeypatch.setattr(lifecycle.Run, "nodes", _RunNodes())
    monkeypatch.setattr(lifecycle, "SandboxService", _SandboxService)
    monkeypatch.setattr(lifecycle, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(
        lifecycle.workspace_service, "build_workspace_spec", lambda sb, **_kw: {}
    )
    return destroyed


async def _prepare(**overrides):
    kwargs = dict(
        run_uid="run-cancelled",
        repository_uid="repo-1",
        intent="fix it",
        target={},
        repo=None,
        adapter=object(),
        chosen_executor=Executor.CLAUDE_CODE,
        chosen_mode=ExecutionMode.IMPLEMENT,
        trigger=RunTrigger.MANUAL,
        triggered_by="tester",
        policy=object(),
        warnings=[],
        provider_uid="",
        context="",
        prepared_sandbox=None,
        sandbox_factory=None,
    )
    kwargs.update(overrides)
    await lifecycle._prepare_dispatch_and_finalize(**kwargs)


async def test_factory_sandbox_is_destroyed_and_recorded_when_run_left_queued(monkeypatch):
    run = _Run(RunStatus.CANCELLED.value)
    destroyed = _harness(monkeypatch, run)

    async def factory():
        return _Sandbox("sb-write-1")

    await _prepare(sandbox_factory=factory)

    # The clone this function made is cleaned up...
    assert destroyed == ["sb-write-1"]
    # ...and the row records which sandbox it owned, so finalize and the audit
    # trail can see it instead of "no workspace".
    assert run.sandbox_uid == "sb-write-1"
    assert run.usage["sandbox_uid"] == "sb-write-1"
    assert run.saves == 1


async def test_discovery_sandbox_is_still_destroyed(monkeypatch):
    run = _Run(RunStatus.CANCELLED.value)
    destroyed = _harness(monkeypatch, run)

    class _SandboxServiceWithCreate:
        async def create_for_discovery(self, **_kw):
            return _Sandbox("sb-discovery-1")

        async def destroy(self, sandbox_uid, *, actor_uid=None):
            destroyed.append(sandbox_uid)

    monkeypatch.setattr(lifecycle, "SandboxService", _SandboxServiceWithCreate)
    monkeypatch.setattr(lifecycle, "repository_to_dto", lambda repo: None)

    await _prepare()

    assert destroyed == ["sb-discovery-1"]
    assert run.sandbox_uid == "sb-discovery-1"


async def test_caller_owned_prepared_sandbox_is_never_destroyed(monkeypatch):
    run = _Run(RunStatus.CANCELLED.value)
    destroyed = _harness(monkeypatch, run)

    await _prepare(prepared_sandbox=_Sandbox("sb-prepared-1"))

    # A prepared sandbox belongs to the caller that passed it in — this
    # function must not tear down someone else's workspace.
    assert destroyed == []
