"""P1 — attention rollup against the real test Neo4j (localhost:7999).

Seeds the current-state sources the service reads (kill switch, PR blockers,
runs, threads, approvals, aggregates) and asserts item presence, ordering,
exclusions, and cross-repo isolation.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from domains.areas.models import Area, AreaEdit
from domains.delivery.models import FindingResolution, PullRequest, Verdict
from domains.findings.models import Finding
from domains.metrics.services.attention_service import attention_service
from domains.repositories.models import Repository
from domains.runs.models import Run
from domains.threads.models import Thread
from domains.tickets.models import EpicProposal, Ticket

pytestmark = pytest.mark.integration


async def _make_repo(*, kill_switch: bool = False) -> Repository:
    r = Repository(
        uid="repo-" + uuid4().hex[:8],
        org_uid="org-" + uuid4().hex[:8],
        slug="s-" + uuid4().hex[:8],
        mode="github",
        name="Test repo",
        kill_switch_active=kill_switch,
    )
    await r.save()
    return r


async def _make_pr(repo_uid: str, number: int, **kwargs) -> PullRequest:
    pr = PullRequest(
        uid=uuid4().hex,
        repository_uid=repo_uid,
        github_number=number,
        pr_key=f"{repo_uid}:{number}",
        title=f"PR {number}",
        state=kwargs.pop("state", "open"),
        **kwargs,
    )
    await pr.save()
    return pr


def _kinds(result) -> list[str]:
    return [i.kind for i in result.items]


async def test_empty_repo_has_no_items():
    repo = await _make_repo()
    result = await attention_service.repo_attention(repo.uid)
    assert result.total == 0
    assert result.items == []


async def test_kill_switch_ranked_first():
    repo = await _make_repo(kill_switch=True)
    await _make_pr(repo.uid, 1, ci_state="red")
    result = await attention_service.repo_attention(repo.uid)
    assert _kinds(result)[0] == "kill_switch_engaged"
    assert "pr_ci_red" in _kinds(result)


async def test_exhausted_orders_before_ci_red_and_drafts_excluded():
    repo = await _make_repo()
    await _make_pr(repo.uid, 1, ci_state="red")
    await _make_pr(repo.uid, 2, fix_rounds_exhausted=True)
    await _make_pr(repo.uid, 3, ci_state="red", draft=True)  # draft: no item
    result = await attention_service.repo_attention(repo.uid)
    kinds = _kinds(result)
    assert kinds.index("pr_fix_rounds_exhausted") < kinds.index("pr_ci_red")
    assert kinds.count("pr_ci_red") == 1


async def test_needs_human_verdict_only_at_head_and_not_superseded():
    repo = await _make_repo()
    pr = await _make_pr(repo.uid, 1, head_sha="abc")

    async def verdict(sha: str, verification_status: str = "") -> Verdict:
        v = Verdict(
            uid=uuid4().hex,
            pull_request_uid=pr.uid,
            repository_uid=repo.uid,
            sha=sha,
            result="needs_human",
            verification_status=verification_status,
        )
        await v.save()
        return v

    await verdict("stale-sha")  # not at head: ignored
    await verdict("abc", verification_status="superseded")  # replaced: ignored
    result = await attention_service.repo_attention(repo.uid)
    assert "verdict_needs_human" not in _kinds(result)

    await verdict("abc")
    result = await attention_service.repo_attention(repo.uid)
    assert _kinds(result).count("verdict_needs_human") == 1
    item = next(i for i in result.items if i.kind == "verdict_needs_human")
    assert item.subject_uid == pr.uid


async def test_waive_requests_counted_per_pr():
    repo = await _make_repo()
    pr = await _make_pr(repo.uid, 1)
    for n in range(2):
        res = FindingResolution(
            uid=uuid4().hex,
            finding_uid=f"f{n}",
            pull_request_uid=pr.uid,
            repository_uid=repo.uid,
            resolution_key=f"f{n}:{pr.uid}",
            waive_requested_by="agent",
            waive_requested_reason="false positive",
        )
        await res.save()
    # Already-waived request does not count.
    waived = FindingResolution(
        uid=uuid4().hex,
        finding_uid="f9",
        pull_request_uid=pr.uid,
        repository_uid=repo.uid,
        resolution_key=f"f9:{pr.uid}",
        waive_requested_by="agent",
        waive_requested_reason="dup",
        state="waived",
    )
    await waived.save()

    result = await attention_service.repo_attention(repo.uid)
    item = next(i for i in result.items if i.kind == "pr_waive_requested")
    assert item.count == 2
    assert item.subject_uid == pr.uid


async def test_recent_bad_runs_counted_old_and_ok_excluded():
    repo = await _make_repo()
    bad = Run(uid=uuid4().hex, repository_uid=repo.uid, executor="internal_llm",
              status="failed", title="broken sweep")
    await bad.save()
    ok = Run(uid=uuid4().hex, repository_uid=repo.uid, executor="internal_llm",
             status="ended")
    await ok.save()
    old = Run(uid=uuid4().hex, repository_uid=repo.uid, executor="internal_llm",
              status="failed", created_at=datetime.now(UTC) - timedelta(days=2))
    await old.save()

    result = await attention_service.repo_attention(repo.uid)
    items = [i for i in result.items if i.kind == "run_needs_attention"]
    assert [i.subject_uid for i in items] == [bad.uid]


async def test_thread_states_counted_ended_phases_excluded():
    repo = await _make_repo()
    ticket = Ticket(uid=uuid4().hex, repository_uid=repo.uid, title="Fix the thing")
    await ticket.save()
    drafted = Thread(uid=uuid4().hex, repository_uid=repo.uid,
                     subject_ticket_uid=ticket.uid, plan_state="drafted")
    await drafted.save()
    ready = Thread(uid=uuid4().hex, repository_uid=repo.uid,
                   subject_ticket_uid=ticket.uid, phase="implementing",
                   ready_for_review=True)
    await ready.save()
    done = Thread(uid=uuid4().hex, repository_uid=repo.uid,
                  subject_ticket_uid=ticket.uid, phase="done",
                  plan_state="drafted", ready_for_review=True)
    await done.save()

    result = await attention_service.repo_attention(repo.uid)
    kinds = _kinds(result)
    assert kinds.count("thread_plan_awaiting_approval") == 1
    assert kinds.count("thread_ready_for_review") == 1
    item = next(i for i in result.items if i.kind == "thread_plan_awaiting_approval")
    assert item.description == "Fix the thing"


async def test_finding_aggregate_excludes_closed_and_proposals():
    repo = await _make_repo()

    async def finding(kind: str, severity: str, status: str) -> None:
        f = Finding(uid=uuid4().hex, repository_uid=repo.uid, kind=kind,
                    severity=severity, status=status, title="t",
                    dedupe_key=uuid4().hex)
        await f.save()

    await finding("defect", "critical", "open")
    await finding("defect", "high", "acknowledged")
    await finding("defect", "high", "dismissed")  # closed: excluded
    await finding("proposal", "critical", "open")  # proposal kind: excluded
    await finding("defect", "low", "open")  # low severity: excluded

    result = await attention_service.repo_attention(repo.uid)
    item = next(i for i in result.items if i.kind == "high_severity_findings")
    assert item.count == 2


async def test_pending_approvals_and_stale_areas_aggregates():
    repo = await _make_repo()
    edit = AreaEdit(uid=uuid4().hex, repository_uid=repo.uid, status="pending")
    await edit.save()
    resolved = AreaEdit(uid=uuid4().hex, repository_uid=repo.uid, status="accepted")
    await resolved.save()
    epic = EpicProposal(uid=uuid4().hex, repository_uid=repo.uid, title="Epic")
    await epic.save()

    now = datetime.now(UTC)
    stale = Area(uid=uuid4().hex, repository_uid=repo.uid, key="backend",
                 code_changed_at=now, last_reviewed_at=now - timedelta(days=1))
    await stale.save()
    fresh = Area(uid=uuid4().hex, repository_uid=repo.uid, key="frontend",
                 code_changed_at=now - timedelta(days=1), last_reviewed_at=now)
    await fresh.save()
    never_changed = Area(uid=uuid4().hex, repository_uid=repo.uid, key="docs")
    await never_changed.save()
    disabled = Area(uid=uuid4().hex, repository_uid=repo.uid, key="old",
                    enabled=False, code_changed_at=now)
    await disabled.save()

    result = await attention_service.repo_attention(repo.uid)
    by_kind = {i.kind: i for i in result.items}
    assert by_kind["area_edits_pending"].count == 1
    assert by_kind["epic_proposals_pending"].count == 1
    assert by_kind["stale_areas"].count == 1


async def test_cross_repo_isolation():
    repo = await _make_repo()
    other = await _make_repo(kill_switch=True)
    await _make_pr(other.uid, 1, ci_state="red")
    edit = AreaEdit(uid=uuid4().hex, repository_uid=other.uid, status="pending")
    await edit.save()

    result = await attention_service.repo_attention(repo.uid)
    assert result.total == 0
