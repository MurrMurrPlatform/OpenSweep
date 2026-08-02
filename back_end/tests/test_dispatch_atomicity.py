"""B6 — dispatch atomicity across processes (run_dispatch.dispatch_serialized).

The in-flight guard reads "is there already an active write run for this target?"
then dispatches — separated by awaits. Two concurrent dispatches for the SAME
target (e.g. the backend webhook and the worker's auto-fix chain) must not both
pass the guard and double-dispatch. dispatch_serialized now holds a cross-process
`dist_lock` across the read+dispatch, so the second caller sees the first's run
and 409s instead of racing a second run onto the branch.
"""

import asyncio

import pytest
from fastapi import HTTPException

from domains.delivery.services import run_dispatch

pytestmark = pytest.mark.asyncio


async def test_concurrent_dispatch_same_target_dispatches_once(monkeypatch):
    active: list[str] = []

    async def fake_active_runs_for(**_kw):
        return list(active)

    def fake_blocking_run(runs, *, playbook):
        return runs[0] if runs else None

    monkeypatch.setattr(run_dispatch, "active_runs_for", fake_active_runs_for)
    monkeypatch.setattr(run_dispatch, "blocking_run", fake_blocking_run)
    monkeypatch.setattr(run_dispatch, "conflict_detail", lambda msg, run: msg)

    dispatched: list[str] = []

    async def dispatch():
        # Hold the lock long enough that the sibling call is forced to wait,
        # then register the run so the sibling's guard sees a conflict.
        await asyncio.sleep(0.05)
        active.append("run-1")
        dispatched.append("run-1")
        return "run-1"

    async def call():
        return await run_dispatch.dispatch_serialized(
            target_uid="pr-race-1",
            playbook="fix",
            conflict_message="busy",
            active_filter={"pull_request_uid": "pr-race-1"},
            dispatch=dispatch,
        )

    results = await asyncio.gather(call(), call(), return_exceptions=True)

    ok = [r for r in results if not isinstance(r, Exception)]
    errs = [r for r in results if isinstance(r, HTTPException)]

    # Exactly one dispatch actually ran; the other was refused with a 409.
    assert dispatched == ["run-1"]
    assert ok == ["run-1"]
    assert len(errs) == 1 and errs[0].status_code == 409


async def test_distinct_targets_dispatch_concurrently(monkeypatch):
    async def fake_active_runs_for(**_kw):
        return []

    def fake_blocking_run(runs, *, playbook):
        return None

    monkeypatch.setattr(run_dispatch, "active_runs_for", fake_active_runs_for)
    monkeypatch.setattr(run_dispatch, "blocking_run", fake_blocking_run)

    async def make(uid):
        async def dispatch():
            return uid

        return await run_dispatch.dispatch_serialized(
            target_uid=uid,
            playbook="fix",
            conflict_message="busy",
            active_filter={"pull_request_uid": uid},
            dispatch=dispatch,
        )

    # Different targets never contend on the same lock key.
    results = await asyncio.gather(make("pr-a"), make("pr-b"))
    assert sorted(results) == ["pr-a", "pr-b"]
