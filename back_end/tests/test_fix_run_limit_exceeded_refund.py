"""finalize_fix_run — LIMIT_EXCEEDED refund (integration, needs test Neo4j).

A fix round is burned optimistically at dispatch time (PullRequest.fix_rounds
incremented before the agent runs). If the run then hits its wall/turn budget
(LIMIT_EXCEEDED) before committing anything, that round must be refunded —
the same way a sandbox-prep failure already is — instead of silently costing
the PR a round it never got to spend.
"""

from uuid import uuid4

import pytest

from domains.delivery.services import fix_run_service, run_dispatch, write_gate
from domains.delivery.models import PullRequest
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


async def _make_pr(repository_uid: str, *, fix_rounds: int) -> PullRequest:
    pr = PullRequest(
        uid=uuid4().hex,
        repository_uid=repository_uid,
        github_number=1,
        pr_key=f"{repository_uid}:1",
        state="open",
        head_ref="feature/x",
        base_ref="main",
        fix_rounds=fix_rounds,
    )
    await pr.save()
    return pr


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


async def _make_run(*, repository_uid: str, sandbox_uid: str, pr_uid: str, status: str) -> Run:
    run = Run(
        uid=uuid4().hex,
        repository_uid=repository_uid,
        playbook="fix",
        executor="claude_code",
        execution_mode="implement",
        status=status,
        sandbox_uid=sandbox_uid,
        linked_pr_uid=pr_uid,
    )
    await run.save()
    return run


@pytest.mark.asyncio
async def test_limit_exceeded_with_no_commits_refunds_fix_round(monkeypatch):
    repo = await _make_repo()
    pr = await _make_pr(repo.uid, fix_rounds=1)  # burned optimistically at dispatch
    sandbox = await _make_sandbox(repo.uid)
    run = await _make_run(
        repository_uid=repo.uid,
        sandbox_uid=sandbox.uid,
        pr_uid=pr.uid,
        status=RunStatus.LIMIT_EXCEEDED.value,
    )

    no_commits_result = write_gate.WriteGateResult(
        ok=False,
        changed_paths=[],
        violations=[write_gate.NO_COMMITS_VIOLATION],
        commits=0,
        work_branch=pr.head_ref,
    )

    async def _fake_validate(*args, **kwargs):
        return no_commits_result

    monkeypatch.setattr(write_gate, "validate_sandbox_changes", _fake_validate)

    await fix_run_service.finalize_fix_run(run)

    refreshed = await PullRequest.nodes.get(uid=pr.uid)
    assert int(refreshed.fix_rounds) == 0  # refunded

    events = await Event.nodes.filter(kind="fix_run.round_refunded")
    assert len([e for e in events if e.subject_uid == pr.uid]) == 1


@pytest.mark.asyncio
async def test_limit_exceeded_with_commits_does_not_refund(monkeypatch):
    repo = await _make_repo()
    pr = await _make_pr(repo.uid, fix_rounds=1)
    sandbox = await _make_sandbox(repo.uid)
    run = await _make_run(
        repository_uid=repo.uid,
        sandbox_uid=sandbox.uid,
        pr_uid=pr.uid,
        status=RunStatus.LIMIT_EXCEEDED.value,
    )

    ok_result = write_gate.WriteGateResult(
        ok=True, changed_paths=["a.py"], violations=[], commits=1, work_branch=pr.head_ref
    )

    async def _fake_validate(*args, **kwargs):
        return ok_result

    async def _fake_push(*args, **kwargs):
        return None

    async def _fake_credentials(*args, **kwargs):
        return "token"

    async def _fake_refresh(*args, **kwargs):
        return None

    async def _fake_sync(*args, **kwargs):
        return None

    monkeypatch.setattr(write_gate, "validate_sandbox_changes", _fake_validate)
    monkeypatch.setattr(write_gate, "push_work_branch", _fake_push)
    monkeypatch.setattr(run_dispatch, "get_git_credentials", _fake_credentials)
    monkeypatch.setattr(
        "domains.agents.services.event_triggers.refresh_docs_for_change", _fake_refresh
    )
    monkeypatch.setattr(
        "domains.delivery.services.pull_request_service.PullRequestService.sync_from_github",
        _fake_sync,
    )

    await fix_run_service.finalize_fix_run(run)

    refreshed = await PullRequest.nodes.get(uid=pr.uid)
    assert int(refreshed.fix_rounds) == 1  # NOT refunded — real work was pushed

    events = await Event.nodes.filter(kind="fix_run.round_refunded")
    assert [e for e in events if e.subject_uid == pr.uid] == []
