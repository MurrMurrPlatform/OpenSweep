"""Platform-tool ticket DTO validation — the executor-facing contract.

`PlatformCreateTicketRequest` and `PlatformUpdateTicketRequest` are the wire
shape agents call. They deliberately differ from the human `Create/Update`
DTOs: origin/status are FORCED (agents may only propose backlog tickets), and
update fields are all optional/None-sentinel (partial patches). If a
validation clause drifts, the executor surface silently starts accepting
things the platform later refuses at the service layer.
"""

import pytest
from pydantic import ValidationError

from api.v1.platform_tools_tickets import (
    PlatformApproveEpicRequest,
    PlatformCreateTicketRequest,
    PlatformProposeEpicRequest,
    PlatformTransitionTicketRequest,
    PlatformUpdateTicketRequest,
)


# ── Create ───────────────────────────────────────────────────────────────────


def test_create_requires_title_and_repository_uid():
    with pytest.raises(ValidationError):
        PlatformCreateTicketRequest(title="", repository_uid="repo1")
    with pytest.raises(ValidationError):
        PlatformCreateTicketRequest(title="fix login", repository_uid="")


def test_create_defaults_priority_and_optional_lists():
    req = PlatformCreateTicketRequest(title="fix login", repository_uid="repo1")
    assert req.priority == "medium"
    assert req.acceptance_criteria == []
    assert req.labels == []
    assert req.size == ""
    assert req.origin_finding_uid == ""
    assert req.parent_ticket_uid == ""


def test_create_carries_acceptance_criteria_and_evidence():
    req = PlatformCreateTicketRequest(
        title="Fix login flakiness",
        repository_uid="repo1",
        description="401s under load",
        acceptance_criteria=["logins succeed", "no 401 with valid cookie"],
        labels=["auth", "flake"],
        priority="high",
        origin_finding_uid="f1",
    )
    assert req.acceptance_criteria == ["logins succeed", "no 401 with valid cookie"]
    assert req.labels == ["auth", "flake"]
    assert req.priority == "high"
    assert req.origin_finding_uid == "f1"


def test_create_does_not_accept_origin_or_status_fields():
    """The forced origin (`agent-proposal`) and starting status (`backlog`)
    are set by the route — the request must not carry an override. Pydantic
    ignores unknown fields by default, so the guarantee is that `origin` /
    `status` DO NOT end up on the DTO."""
    req = PlatformCreateTicketRequest(
        title="x",
        repository_uid="repo1",
        origin="human",  # ignored
        status="todo",  # ignored
    )
    assert not hasattr(req, "origin")
    assert not hasattr(req, "status")


# ── Update ───────────────────────────────────────────────────────────────────


def test_update_requires_ticket_uid():
    with pytest.raises(ValidationError):
        PlatformUpdateTicketRequest(ticket_uid="")


def test_update_all_patch_fields_default_none():
    """Every editable field defaults to None so the service layer can
    distinguish "no change" from "clear the value" — used with model_dump(
    exclude_none=True) in the route."""
    req = PlatformUpdateTicketRequest(ticket_uid="t1")
    dumped = req.model_dump(exclude={"ticket_uid"}, exclude_none=True)
    assert dumped == {}


def test_update_partial_patch_drops_unset_fields():
    req = PlatformUpdateTicketRequest(
        ticket_uid="t1", acceptance_criteria=["a", "b"], priority="high"
    )
    dumped = req.model_dump(exclude={"ticket_uid"}, exclude_none=True)
    assert dumped == {"acceptance_criteria": ["a", "b"], "priority": "high"}


def test_update_title_may_not_be_empty_string():
    """`title=None` is a no-op patch; title="" would erase the title on
    save, so the min_length guard forces callers to omit rather than clear."""
    with pytest.raises(ValidationError):
        PlatformUpdateTicketRequest(ticket_uid="t1", title="")


# ── Propose group (epic) ─────────────────────────────────────────────────────


def test_propose_epic_requires_at_least_two_members():
    with pytest.raises(ValidationError):
        PlatformProposeEpicRequest(
            repository_uid="repo1",
            title="Consolidate auth",
            member_ticket_uids=["t1"],
        )
    # Zero members also rejected.
    with pytest.raises(ValidationError):
        PlatformProposeEpicRequest(
            repository_uid="repo1",
            title="Consolidate auth",
            member_ticket_uids=[],
        )


def test_propose_epic_defaults_axis_to_root_cause():
    req = PlatformProposeEpicRequest(
        repository_uid="repo1",
        title="Consolidate auth",
        member_ticket_uids=["t1", "t2"],
    )
    assert req.axis == "root-cause"
    assert req.evidence == {}
    assert req.suggested_labels == []
    assert req.suggested_priority == "medium"
    assert req.rationale == ""


def test_propose_epic_carries_evidence_and_labels():
    req = PlatformProposeEpicRequest(
        repository_uid="repo1",
        title="Consolidate auth",
        rationale="one bug, three symptoms",
        member_ticket_uids=["t1", "t2", "t3"],
        axis="root-cause",
        evidence={"root_cause": "unawait close()"},
        suggested_labels=["auth", "concurrency"],
        suggested_priority="high",
    )
    assert req.member_ticket_uids == ["t1", "t2", "t3"]
    assert req.evidence == {"root_cause": "unawait close()"}
    assert req.suggested_labels == ["auth", "concurrency"]
    assert req.suggested_priority == "high"


# ── Transition + approve ─────────────────────────────────────────────────────


def test_transition_requires_ticket_uid_and_target_status():
    with pytest.raises(ValidationError):
        PlatformTransitionTicketRequest(ticket_uid="", to_status="todo")
    with pytest.raises(ValidationError):
        PlatformTransitionTicketRequest(ticket_uid="t1", to_status="")


def test_approve_epic_requires_proposal_uid():
    with pytest.raises(ValidationError):
        PlatformApproveEpicRequest(proposal_uid="")
