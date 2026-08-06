"""Sweep in-flight guards must be atomic across the read+dispatch.

`generate-docs`, `generate-specs` and `deep-scan` each promise "one run of this
kind per repository at a time", but the guard used to be a bare check-then-act:
read `active_runs_for`, await, then let `trigger_run` create the Run. Two
near-simultaneous requests (a double-click, two tabs, a retried POST) both saw
"nothing in flight" and both dispatched a full LLM run. The guard now holds a
cross-process `dist_lock` across the read AND the dispatch, so the loser sees
the winner's queued run and 409s.
"""

import asyncio

import pytest
from fastapi import HTTPException

from api.v1 import sweep as sweep_api

pytestmark = pytest.mark.asyncio


class _Agent:
    uid = "agent-generate-docs"


class _Run:
    def __init__(self, uid):
        self.uid = uid
        self.agent_uid = _Agent.uid
        self.status = "queued"


@pytest.fixture
def guard_env(monkeypatch):
    """Stub the agent registry / active-run lookup behind the guard."""
    active: list[_Run] = []

    async def fake_active_runs_for(**_kw):
        return list(active)

    async def fake_system_agent_by_key(_key):
        return _Agent()

    monkeypatch.setattr(sweep_api, "active_runs_for", fake_active_runs_for)
    monkeypatch.setattr(sweep_api, "conflict_detail", lambda msg, run: msg)
    monkeypatch.setattr(
        "domains.agents.services.registry.system_agent_by_key", fake_system_agent_by_key
    )
    return active


async def test_concurrent_generate_docs_dispatches_once(guard_env):
    active = guard_env
    dispatched: list[str] = []

    async def call():
        async with sweep_api._one_sweep_at_a_time("repo-1", "generate-docs", "busy"):
            # Hold the lock long enough that the sibling call has to wait,
            # then register the run so the sibling's guard sees the conflict.
            await asyncio.sleep(0.05)
            active.append(_Run("run-1"))
            dispatched.append("run-1")

    results = await asyncio.gather(call(), call(), return_exceptions=True)
    errs = [r for r in results if isinstance(r, HTTPException)]

    assert dispatched == ["run-1"]
    assert len(errs) == 1 and errs[0].status_code == 409


async def test_guard_409s_when_a_run_of_that_kind_is_already_in_flight(guard_env):
    guard_env.append(_Run("existing"))
    with pytest.raises(HTTPException) as exc:
        async with sweep_api._one_sweep_at_a_time("repo-1", "generate-docs", "busy"):
            pass
    assert exc.value.status_code == 409
    assert exc.value.detail == "busy"


async def test_distinct_repositories_do_not_contend(guard_env):
    entered: list[str] = []

    async def call(repo):
        async with sweep_api._one_sweep_at_a_time(repo, "generate-docs", "busy"):
            await asyncio.sleep(0.02)
            entered.append(repo)

    await asyncio.gather(call("repo-a"), call("repo-b"))
    assert sorted(entered) == ["repo-a", "repo-b"]


async def test_distinct_sweep_kinds_do_not_contend(guard_env):
    entered: list[str] = []

    async def call(kind):
        async with sweep_api._one_sweep_at_a_time("repo-1", kind, "busy"):
            await asyncio.sleep(0.02)
            entered.append(kind)

    # Same repository, different sweep kind — different lock key, so a running
    # generate-docs never serializes an unrelated deep scan.
    await asyncio.gather(call("generate-docs"), call("deep-scan"))
    assert sorted(entered) == ["deep-scan", "generate-docs"]
