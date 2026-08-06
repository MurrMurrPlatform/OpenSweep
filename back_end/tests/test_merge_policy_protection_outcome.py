"""Turning "Enforce the converged check on GitHub" ON must never claim more
than GitHub actually does (integration, needs test Neo4j).

The two most likely repositories are precisely the ones where the write
no-ops: a default branch that already has a protection rule (OpenSweep
refuses to rewrite one, so the context is never added) and a credential
without repo admin. Previously the endpoint saved the toggle ON and threw the
outcome away, so the UI showed an enforced merge gate that did not exist.
"""

from uuid import uuid4

import pytest

from api.v1 import delivery as delivery_routes
from domains.delivery.schemas import UpdateMergePolicyRequest
from domains.events.models import Event
from domains.repositories.models import Repository
from domains.users.schemas import UserDTO

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
        default_branch="main",
    )
    await repo.save()
    return repo


def _user(org_uid: str) -> UserDTO:
    return UserDTO(
        uid=uuid4().hex,
        email="maintainer@example.com",
        display_name="Maintainer",
        role="maintainer",
        org_uid=org_uid,
    )


async def _flip_on(monkeypatch, repo: Repository, outcome: str):
    """Turn enforcement on with `ensure_converged_status_required` stubbed to
    report `outcome`, and hand back the response DTO.

    The route imports the helper inside the function body, so patching the
    module attribute is what takes effect.
    """

    async def _fake_ensure(r, *, actor_uid="system"):
        return outcome

    monkeypatch.setattr(
        "domains.delivery.services.pull_request_service.ensure_converged_status_required",
        _fake_ensure,
    )
    return await delivery_routes.update_merge_policy(
        repo.uid,
        UpdateMergePolicyRequest(enforce_converged_status=True),
        user=_user(repo.org_uid),
    )


@pytest.mark.asyncio
async def test_existing_rule_left_alone_is_reported_not_swallowed(monkeypatch):
    repo = await _make_repo()

    dto = await _flip_on(monkeypatch, repo, "not-required")

    # The preference is still saved — the user asked for it, and OpenSweep's
    # own convergence gate still honors it…
    assert dto.enforce_converged_status is True
    # …but the response says plainly that GitHub is requiring nothing.
    assert dto.branch_protection_outcome == "not-required"

    events = await Event.nodes.filter(kind="delivery.branch_protection_not_applied")
    matching = [e for e in events if e.payload.get("repository_uid") == repo.uid]
    assert len(matching) == 1
    assert matching[0].payload.get("outcome") == "not-required"


@pytest.mark.asyncio
async def test_missing_admin_rights_is_reported_not_swallowed(monkeypatch):
    repo = await _make_repo()

    dto = await _flip_on(monkeypatch, repo, "failed")

    assert dto.enforce_converged_status is True
    assert dto.branch_protection_outcome == "failed"

    events = await Event.nodes.filter(kind="delivery.branch_protection_not_applied")
    matching = [e for e in events if e.payload.get("repository_uid") == repo.uid]
    assert len(matching) == 1
    assert matching[0].payload.get("outcome") == "failed"


@pytest.mark.asyncio
async def test_rule_created_reports_success_and_audits_nothing_alarming(monkeypatch):
    repo = await _make_repo()

    dto = await _flip_on(monkeypatch, repo, "created")

    assert dto.enforce_converged_status is True
    assert dto.branch_protection_outcome == "created"

    events = await Event.nodes.filter(kind="delivery.branch_protection_not_applied")
    assert [e for e in events if e.payload.get("repository_uid") == repo.uid] == []


@pytest.mark.asyncio
async def test_already_required_reports_success(monkeypatch):
    repo = await _make_repo()

    dto = await _flip_on(monkeypatch, repo, "already-required")

    assert dto.branch_protection_outcome == "already-required"
    events = await Event.nodes.filter(kind="delivery.branch_protection_not_applied")
    assert [e for e in events if e.payload.get("repository_uid") == repo.uid] == []


@pytest.mark.asyncio
async def test_unrelated_policy_edit_does_not_touch_github_or_report_an_outcome():
    """Only the OFF→ON flip may write to the user's repo settings; editing
    max_fix_rounds must not, and must not imply an outcome either."""
    repo = await _make_repo()

    dto = await delivery_routes.update_merge_policy(
        repo.uid,
        UpdateMergePolicyRequest(max_fix_rounds=5),
        user=_user(repo.org_uid),
    )

    assert dto.max_fix_rounds == 5
    assert dto.branch_protection_outcome is None
