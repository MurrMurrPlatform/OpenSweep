"""finalize_write_run — the on_pushed post-push guard (integration, needs test Neo4j).

A transient GitHub error while opening the draft PR (or any other per-service
post-push action) must never strand a pushed work branch silently: the push
already happened and cannot be rolled back, so the failure has to be audited
instead of propagating uncaught.
"""

from uuid import uuid4

import pytest

from domains.delivery.services import run_dispatch, write_gate
from domains.events.models import Event
from domains.execution.models import Sandbox
from domains.repositories.models import Repository
from domains.runs.models import Run
from domains.runs.schemas import RunStatus

pytestmark = pytest.mark.integration


async def _make_repo() -> Repository:
    repo = Repository(
        uid=uuid4().hex,
        org_uid=uuid4().hex,
        slug=f"repo-{uuid4().hex[:8]}",
        mode="github",
        name="test-repo",
        github_owner="acme",
        github_repo="widgets",
    )
    await repo.save()
    return repo


async def _make_sandbox(repository_uid: str) -> Sandbox:
    sb = Sandbox(
        uid=uuid4().hex,
        repository_uid=repository_uid,
        host_path="/tmp/host",
        container_path="/tmp/container",
        purpose="write",
        status="ready",
    )
    await sb.save()
    return sb


async def _make_run(
    *, repository_uid: str, sandbox_uid: str, status: str = RunStatus.AWAITING_INPUT.value
) -> Run:
    run = Run(
        uid=uuid4().hex,
        repository_uid=repository_uid,
        playbook="implement",
        executor="claude_code",
        execution_mode="implement",
        status=status,
        sandbox_uid=sandbox_uid,
    )
    await run.save()
    return run


@pytest.mark.asyncio
async def test_post_push_action_failure_is_audited_not_raised(monkeypatch):
    repo = await _make_repo()
    sandbox = await _make_sandbox(repo.uid)
    run = await _make_run(repository_uid=repo.uid, sandbox_uid=sandbox.uid)

    ok_result = write_gate.WriteGateResult(
        ok=True, changed_paths=["a.py"], violations=[], commits=1, work_branch="opensweep/x"
    )

    async def _fake_validate(*args, **kwargs):
        return ok_result

    async def _fake_push(*args, **kwargs):
        return None

    async def _fake_refresh(*args, **kwargs):
        return None

    async def _fake_credentials(*args, **kwargs):
        return "token"

    monkeypatch.setattr(write_gate, "validate_sandbox_changes", _fake_validate)
    monkeypatch.setattr(write_gate, "push_work_branch", _fake_push)
    monkeypatch.setattr(run_dispatch, "get_git_credentials", _fake_credentials)
    monkeypatch.setattr(
        "domains.agents.services.event_triggers.refresh_docs_for_change", _fake_refresh
    )

    async def _boom(sandbox_dto, result):
        raise RuntimeError("GitHub 502 while opening the draft PR")

    # Must not raise — the branch is already pushed and cannot be undone.
    await run_dispatch.finalize_write_run(
        run,
        audit_prefix="implement_run",
        subject_uid=run.uid,
        subject_type="Run",
        repository_uid=repo.uid,
        base_ref="main",
        work_branch="opensweep/x",
        on_pushed=_boom,
    )

    events = await Event.nodes.filter(kind="implement_run.post_push_failed")
    matching = [e for e in events if e.subject_uid == run.uid]
    assert len(matching) == 1
    assert "GitHub 502" in matching[0].payload.get("error", "")
    assert matching[0].payload.get("work_branch") == "opensweep/x"


@pytest.mark.asyncio
async def test_post_push_action_success_is_not_audited_as_failed(monkeypatch):
    repo = await _make_repo()
    sandbox = await _make_sandbox(repo.uid)
    run = await _make_run(repository_uid=repo.uid, sandbox_uid=sandbox.uid)

    ok_result = write_gate.WriteGateResult(
        ok=True, changed_paths=["a.py"], violations=[], commits=1, work_branch="opensweep/y"
    )

    async def _fake_validate(*args, **kwargs):
        return ok_result

    async def _fake_push(*args, **kwargs):
        return None

    async def _fake_refresh(*args, **kwargs):
        return None

    async def _fake_credentials(*args, **kwargs):
        return "token"

    monkeypatch.setattr(write_gate, "validate_sandbox_changes", _fake_validate)
    monkeypatch.setattr(write_gate, "push_work_branch", _fake_push)
    monkeypatch.setattr(run_dispatch, "get_git_credentials", _fake_credentials)
    monkeypatch.setattr(
        "domains.agents.services.event_triggers.refresh_docs_for_change", _fake_refresh
    )

    called = {"on_pushed": False}

    async def _happy(sandbox_dto, result):
        called["on_pushed"] = True

    await run_dispatch.finalize_write_run(
        run,
        audit_prefix="implement_run",
        subject_uid=run.uid,
        subject_type="Run",
        repository_uid=repo.uid,
        base_ref="main",
        work_branch="opensweep/y",
        on_pushed=_happy,
    )

    assert called["on_pushed"] is True
    events = await Event.nodes.filter(kind="implement_run.post_push_failed")
    matching = [e for e in events if e.subject_uid == run.uid]
    assert matching == []


# ── LIMIT_EXCEEDED must still run the gate — committed work is not discarded ─


@pytest.mark.asyncio
async def test_limit_exceeded_with_commits_still_pushes(monkeypatch):
    """A run that hit its wall/turn budget after committing real work must
    still have that work validated and pushed — LIMIT_EXCEEDED is not the
    same as FAILED."""
    repo = await _make_repo()
    sandbox = await _make_sandbox(repo.uid)
    run = await _make_run(
        repository_uid=repo.uid,
        sandbox_uid=sandbox.uid,
        status=RunStatus.LIMIT_EXCEEDED.value,
    )

    ok_result = write_gate.WriteGateResult(
        ok=True, changed_paths=["a.py"], violations=[], commits=1, work_branch="opensweep/z"
    )

    async def _fake_validate(*args, **kwargs):
        return ok_result

    pushed = {"called": False}

    async def _fake_push(*args, **kwargs):
        pushed["called"] = True

    async def _fake_refresh(*args, **kwargs):
        return None

    async def _fake_credentials(*args, **kwargs):
        return "token"

    monkeypatch.setattr(write_gate, "validate_sandbox_changes", _fake_validate)
    monkeypatch.setattr(write_gate, "push_work_branch", _fake_push)
    monkeypatch.setattr(run_dispatch, "get_git_credentials", _fake_credentials)
    monkeypatch.setattr(
        "domains.agents.services.event_triggers.refresh_docs_for_change", _fake_refresh
    )

    called = {"on_pushed": False}

    async def _on_pushed(sandbox_dto, result):
        called["on_pushed"] = True

    result = await run_dispatch.finalize_write_run(
        run,
        audit_prefix="fix_run",
        subject_uid=run.uid,
        subject_type="Run",
        repository_uid=repo.uid,
        base_ref="main",
        work_branch="opensweep/z",
        on_pushed=_on_pushed,
    )

    assert pushed["called"] is True
    assert called["on_pushed"] is True
    assert result is not None and result.ok is True

    events = await Event.nodes.filter(kind="fix_run.failed")
    assert [e for e in events if e.subject_uid == run.uid] == []


@pytest.mark.asyncio
async def test_limit_exceeded_with_no_commits_blocks_without_pushing(monkeypatch):
    """A run that hit its budget before committing anything is still
    reported as blocked (no commits) — never pushed, never treated as a
    silent success."""
    repo = await _make_repo()
    sandbox = await _make_sandbox(repo.uid)
    run = await _make_run(
        repository_uid=repo.uid,
        sandbox_uid=sandbox.uid,
        status=RunStatus.LIMIT_EXCEEDED.value,
    )

    no_commits_result = write_gate.WriteGateResult(
        ok=False,
        changed_paths=[],
        violations=[write_gate.NO_COMMITS_VIOLATION],
        commits=0,
        work_branch="opensweep/z",
    )

    async def _fake_validate(*args, **kwargs):
        return no_commits_result

    async def _never_push(*args, **kwargs):
        raise AssertionError("must not push when there are no commits")

    monkeypatch.setattr(write_gate, "validate_sandbox_changes", _fake_validate)
    monkeypatch.setattr(write_gate, "push_work_branch", _never_push)

    async def _on_pushed(sandbox_dto, result):
        raise AssertionError("on_pushed must not fire without a push")

    result = await run_dispatch.finalize_write_run(
        run,
        audit_prefix="fix_run",
        subject_uid=run.uid,
        subject_type="Run",
        repository_uid=repo.uid,
        base_ref="main",
        work_branch="opensweep/z",
        on_pushed=_on_pushed,
    )

    assert result is not None and result.ok is False
    events = await Event.nodes.filter(kind="fix_run.blocked")
    assert len([e for e in events if e.subject_uid == run.uid]) == 1
