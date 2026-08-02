"""B8 — execute_queued_run idempotency under acks_late.

The run row stays QUEUED across the whole sandbox clone (RUNNING is only stamped
after the clone lands), so a redelivered `dispatch_run` task — worker crash or
Celery visibility-timeout re-queue — would pass the QUEUED check and clone +
dispatch a SECOND agent onto the same run. execute_queued_run now holds a
per-run `dist_lock`, so exactly one task dispatches; the duplicate is skipped.
"""

import asyncio

import pytest

from domains.runs.schemas import RunStatus
from domains.runs.services import lifecycle

pytestmark = pytest.mark.asyncio


class _FakeRun:
    def __init__(self, uid: str):
        self.uid = uid
        self.status = RunStatus.QUEUED.value
        self.usage = {}
        self.target = {}
        self.repository_uid = ""
        self.run_policy_uid = None
        self.executor = "claude_code"
        self.execution_mode = None
        self.trigger = None
        self.triggered_by = ""
        self.provider_uid = ""
        self.effort = ""
        self.reasoning = ""


def _patch_common(monkeypatch, run: _FakeRun):
    class _RunNodes:
        async def get_or_none(self, uid: str):
            return run if uid == run.uid else None

    class _RepoNodes:
        async def get_or_none(self, uid: str):
            return None

    monkeypatch.setattr(lifecycle.Run, "nodes", _RunNodes())
    monkeypatch.setattr(lifecycle.Repository, "nodes", _RepoNodes())

    async def _limits(_run):
        return (10, 20)

    monkeypatch.setattr(
        "domains.runs.tasks.task_limits.limits_for_run", _limits
    )

    async def _brief(**_kw):
        return ""

    monkeypatch.setattr(lifecycle, "_load_briefing", _brief)
    monkeypatch.setattr(
        lifecycle.AdapterRegistry, "get", staticmethod(lambda _e: object())
    )


async def test_concurrent_dispatch_runs_once(monkeypatch):
    run = _FakeRun("run-q8")
    _patch_common(monkeypatch, run)

    calls: list[str] = []

    async def _fake_prepare(**kw):
        calls.append(kw["run_uid"])
        await asyncio.sleep(0.05)  # still cloning while the redelivery fires
        run.status = RunStatus.RUNNING.value

    monkeypatch.setattr(lifecycle, "_prepare_dispatch_and_finalize", _fake_prepare)

    results = await asyncio.gather(
        lifecycle.execute_queued_run("run-q8"),
        lifecycle.execute_queued_run("run-q8"),
    )

    assert calls == ["run-q8"]  # exactly one clone/dispatch
    assert sorted(r["outcome"] for r in results) == ["dispatched", "skipped"]


async def test_non_queued_run_is_skipped(monkeypatch):
    run = _FakeRun("run-q9")
    run.status = RunStatus.RUNNING.value
    _patch_common(monkeypatch, run)

    async def _fake_prepare(**_kw):
        raise AssertionError("should not dispatch a non-QUEUED run")

    monkeypatch.setattr(lifecycle, "_prepare_dispatch_and_finalize", _fake_prepare)

    out = await lifecycle.execute_queued_run("run-q9")
    assert out["outcome"] == "skipped"
