"""B7 — quota resume lease (resume_paused._resume_by_uid).

`redispatch_run` only flips a run off `paused_quota` after re-cloning the
workspace, so the 10-min beat could enqueue a second `resume_run` for the same
run while the first is still cloning — a double dispatch. `_resume_by_uid` now
holds a cross-process `dist_lock("resume:<uid>")` for the whole resume, so a
concurrent resume for the same run is skipped instead of double-dispatching.
"""

import asyncio

import pytest

from domains.runs.models import Run
from domains.runs.tasks import resume_paused

pytestmark = pytest.mark.asyncio


class _FakeRun:
    def __init__(self, uid: str, status: str = "paused_quota"):
        self.uid = uid
        self.status = status


def _install_fake_run(monkeypatch, run: _FakeRun):
    class _FakeNodes:
        async def get_or_none(self, uid: str):
            return run if uid == run.uid else None

    monkeypatch.setattr(Run, "nodes", _FakeNodes())

    async def _limits(_run):
        return (10, 20)

    monkeypatch.setattr(resume_paused, "limits_for_run", _limits)


async def test_concurrent_resume_dispatches_once(monkeypatch):
    run = _FakeRun("run-lease-1")
    _install_fake_run(monkeypatch, run)

    calls: list[str] = []

    async def _fake_resume_one(r, *, now):
        calls.append(r.uid)
        await asyncio.sleep(0.05)  # still "cloning" while the sibling tick fires
        r.status = "running"  # redispatch flips the row off paused_quota
        return "resumed"

    monkeypatch.setattr(resume_paused, "_resume_one", _fake_resume_one)

    results = await asyncio.gather(
        resume_paused._resume_by_uid("run-lease-1"),
        resume_paused._resume_by_uid("run-lease-1"),
    )

    assert calls == ["run-lease-1"]  # exactly one real resume
    assert sorted(r["outcome"] for r in results) == ["resumed", "skipped"]


async def test_lease_released_after_resume(monkeypatch):
    run = _FakeRun("run-lease-2")
    _install_fake_run(monkeypatch, run)

    async def _fake_resume_one(r, *, now):
        # A WAIT outcome leaves the run paused — the lease must release so a
        # later tick can re-evaluate it.
        return "waiting"

    monkeypatch.setattr(resume_paused, "_resume_one", _fake_resume_one)

    first = await resume_paused._resume_by_uid("run-lease-2")
    assert first["outcome"] == "waiting"
    # Lease is free again → a subsequent tick re-enters (not stuck on skipped).
    second = await resume_paused._resume_by_uid("run-lease-2")
    assert second["outcome"] == "waiting"
