"""G2 — the in-app merge button (PullRequestService.merge).

Merging is the README's second promised human action. The service recomputes
convergence at head first, refuses a non-converged PR (409) unless a maintainer
overrides, merges via the git provider, flips the mirror to merged, and completes
the linked ticket (Gate-2).
"""

import pytest
from fastapi import HTTPException

from domains.delivery.schemas import ConvergenceState
from domains.delivery.services import pull_request_service as prs

pytestmark = pytest.mark.asyncio


class _FakePR:
    def __init__(self, ticket_uid: str = "t1"):
        self.uid = "pr1"
        self.state = "open"
        self.repository_uid = "r1"
        self.github_number = 7
        self.ticket_uid = ticket_uid
        self.updated_at = None
        self.saved = False

    async def save(self):
        self.saved = True


class _FakeRepo:
    github_owner = "octo"
    github_repo = "app"


class _FakeClient:
    is_active = True

    def __init__(self, result):
        self._result = result
        self.calls: list[tuple] = []

    async def merge_pull_request(self, owner, repo, number, *, method="squash", **_kw):
        self.calls.append((owner, repo, number, method))
        return self._result


class _FakePolicy:
    merge_method = "squash"


def _patch(monkeypatch, *, converged: bool, client: _FakeClient, method_policy="squash"):
    async def _repo_and_client(self, _repository_uid):
        return _FakeRepo(), client

    async def _recompute(self, _pr):
        return ConvergenceState(
            converged=converged, reasons=[] if converged else ["CI is red"]
        )

    policy = _FakePolicy()
    policy.merge_method = method_policy

    async def _ensure_policy(_repo_uid):
        return policy

    async def _audit(**_kw):
        return None

    completed: dict = {}

    class _FakeTicketService:
        async def mark_done_via_merge(self, uid, *, pull_request_uid=""):
            completed["ticket"] = (uid, pull_request_uid)

    async def _note(_pr_uid):
        completed["noted"] = _pr_uid

    monkeypatch.setattr(prs.PullRequestService, "_repo_and_client", _repo_and_client)
    monkeypatch.setattr(prs.PullRequestService, "recompute", _recompute)
    monkeypatch.setattr(prs, "ensure_merge_policy", _ensure_policy)
    monkeypatch.setattr(prs, "write_audit", _audit)
    monkeypatch.setattr(prs, "pull_request_to_dto", lambda pr: {"uid": pr.uid, "state": pr.state})
    monkeypatch.setattr(
        "domains.tickets.services.ticket_service.TicketService", _FakeTicketService
    )
    monkeypatch.setattr("domains.threads.services.hooks.note_pr_merged", _note)
    return completed


async def test_merge_when_converged_succeeds_and_completes_ticket(monkeypatch):
    client = _FakeClient({"merged": True, "sha": "deadbeef"})
    completed = _patch(monkeypatch, converged=True, client=client)
    pr = _FakePR()

    dto = await prs.PullRequestService().merge(pr, actor_uid="u1")

    assert pr.state == "merged" and pr.saved is True
    assert dto == {"uid": "pr1", "state": "merged"}
    # Squash merge issued with the PR's coordinates.
    assert client.calls == [("octo", "app", 7, "squash")]
    # Linked ticket completed via the system move.
    assert completed["ticket"] == ("t1", "pr1")
    assert completed["noted"] == "pr1"


async def test_merge_when_not_converged_is_409(monkeypatch):
    client = _FakeClient({"merged": True, "sha": "x"})
    _patch(monkeypatch, converged=False, client=client)
    pr = _FakePR()

    with pytest.raises(HTTPException) as exc:
        await prs.PullRequestService().merge(pr, actor_uid="u1")
    assert exc.value.status_code == 409
    assert not client.calls  # never reached GitHub


async def test_override_merges_non_converged(monkeypatch):
    client = _FakeClient({"merged": True, "sha": "x"})
    _patch(monkeypatch, converged=False, client=client)
    pr = _FakePR()

    dto = await prs.PullRequestService().merge(pr, actor_uid="u1", override=True)
    assert dto["state"] == "merged"
    assert client.calls == [("octo", "app", 7, "squash")]


async def test_invalid_method_is_422(monkeypatch):
    client = _FakeClient({"merged": True, "sha": "x"})
    _patch(monkeypatch, converged=True, client=client)
    pr = _FakePR()

    with pytest.raises(HTTPException) as exc:
        await prs.PullRequestService().merge(pr, actor_uid="u1", method="fast-forward")
    assert exc.value.status_code == 422
    assert not client.calls


async def test_github_refusal_is_409(monkeypatch):
    client = _FakeClient({"merged": False, "message": "Pull Request is not mergeable"})
    _patch(monkeypatch, converged=True, client=client)
    pr = _FakePR()

    with pytest.raises(HTTPException) as exc:
        await prs.PullRequestService().merge(pr, actor_uid="u1")
    assert exc.value.status_code == 409
    assert "not mergeable" in exc.value.detail
    assert pr.state == "open"  # mirror not flipped on a refused merge


async def test_already_merged_is_409(monkeypatch):
    client = _FakeClient({"merged": True, "sha": "x"})
    _patch(monkeypatch, converged=True, client=client)
    pr = _FakePR()
    pr.state = "merged"

    with pytest.raises(HTTPException) as exc:
        await prs.PullRequestService().merge(pr, actor_uid="u1")
    assert exc.value.status_code == 409
    assert not client.calls


async def test_policy_method_used_when_request_omits(monkeypatch):
    client = _FakeClient({"merged": True, "sha": "x"})
    _patch(monkeypatch, converged=True, client=client, method_policy="merge")
    pr = _FakePR()

    await prs.PullRequestService().merge(pr, actor_uid="u1")
    assert client.calls == [("octo", "app", 7, "merge")]
