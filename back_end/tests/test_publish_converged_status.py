"""PullRequestService._publish_status — what `opensweep/converged` reports.

Two properties matter here, both because the status can be a REQUIRED branch
protection check:

  1. A PR OpenSweep does not gate must be reported `success`. `sync_repository`
     mirrors EVERY open PR, including human-authored ones, and a human PR can
     never obtain the fresh approving verdict `compute_convergence` requires —
     so reporting it pending/failure would wedge it permanently.
  2. A missing credential must leave an audit trail, not just a log line: the
     user's symptom is "the status never appeared", with nothing to explain it.
"""

from types import SimpleNamespace

import pytest

from domains.delivery.schemas import ConvergenceCounts, ConvergenceState
from domains.delivery.services import pull_request_service
from domains.delivery.services.pull_request_service import PullRequestService


def _pr(**overrides) -> SimpleNamespace:
    defaults = dict(
        uid="pr-1", pr_key="repo-1:7", state="open", head_sha="a" * 40,
        github_number=7, ticket_uid="",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _repo() -> SimpleNamespace:
    return SimpleNamespace(uid="repo-1", github_owner="acme", github_repo="widgets")


def _state(**overrides) -> ConvergenceState:
    defaults = dict(
        converged=False, head_sha="a" * 40, counts=ConvergenceCounts(),
        reasons=["no verdict recorded"],
    )
    defaults.update(overrides)
    return ConvergenceState(**defaults)


class _Client:
    is_active = True

    def __init__(self):
        self.published = []

    async def create_commit_status(self, owner, repo, sha, **kw):
        self.published.append(kw)
        return {}


@pytest.mark.asyncio
async def test_non_opensweep_pr_is_reported_success_not_wedged(monkeypatch):
    client = _Client()
    monkeypatch.setattr(pull_request_service, "get_provider_client", lambda r: client)

    service = PullRequestService()
    monkeypatch.setattr(service, "is_gated", lambda pr: _false())

    await service._publish_status(_repo(), _pr(), _state())

    assert len(client.published) == 1
    assert client.published[0]["state"] == "success"
    assert "nothing to gate" in client.published[0]["description"]


@pytest.mark.asyncio
async def test_gated_pr_without_a_verdict_stays_pending(monkeypatch):
    client = _Client()
    monkeypatch.setattr(pull_request_service, "get_provider_client", lambda r: client)

    service = PullRequestService()
    monkeypatch.setattr(service, "is_gated", lambda pr: _true())

    await service._publish_status(_repo(), _pr(ticket_uid="t-1"), _state())

    assert client.published[0]["state"] == "pending"


@pytest.mark.asyncio
async def test_gated_pr_with_blocking_findings_is_failure(monkeypatch):
    client = _Client()
    monkeypatch.setattr(pull_request_service, "get_provider_client", lambda r: client)

    service = PullRequestService()
    monkeypatch.setattr(service, "is_gated", lambda pr: _true())

    await service._publish_status(
        _repo(), _pr(ticket_uid="t-1"), _state(counts=ConvergenceCounts(blocking=2))
    )

    assert client.published[0]["state"] == "failure"


@pytest.mark.asyncio
async def test_missing_credential_is_audited_not_just_logged(monkeypatch):
    """The gap the user notices last: no status ever appears and nothing says why."""
    audits = []

    async def _fake_audit(**kwargs):
        audits.append(kwargs)

    class _Inactive:
        is_active = False

    monkeypatch.setattr(pull_request_service, "get_provider_client", lambda r: _Inactive())
    monkeypatch.setattr(pull_request_service, "write_audit", _fake_audit)

    await PullRequestService()._publish_status(_repo(), _pr(), _state())

    assert [a["kind"] for a in audits] == ["delivery.credential_missing"]
    assert audits[0]["payload"]["action"] == "publish_converged_status"
    assert audits[0]["subject_uid"] == "pr-1"


@pytest.mark.asyncio
async def test_credential_missing_audit_maps_to_a_notification():
    from domains.notifications.catalog import _KIND_MAP

    assert _KIND_MAP["delivery.credential_missing"] == ("attention.required",)
    assert _KIND_MAP["delivery.branch_protection_created"] == ("attention.required",)


@pytest.mark.asyncio
async def test_audit_failure_never_breaks_the_caller(monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(pull_request_service, "write_audit", _boom)
    # Must not raise.
    await pull_request_service._audit_credential_missing(_pr(), _repo(), action="x")


async def _true() -> bool:
    return True


async def _false() -> bool:
    return False
