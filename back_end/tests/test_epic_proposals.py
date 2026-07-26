"""Pure-function tests for ticket grouping — intent builder + DTO mapping."""

from api.v1.tickets import GROUPING_AGGRESSIVENESS, _build_epic_proposal_intent
from domains.tickets.models import EPIC_PROPOSAL_STATUSES, EpicProposal
from domains.tickets.schemas import EpicProposalStatus, TicketDTO, TicketStatus
from domains.tickets.services.epic_service import proposal_to_dto
from domains.tickets.services.epics.schemas import MAX_RATIONALE_CHARS


def _ticket(uid: str, *, title: str = "t", status: str = "backlog", description: str = "") -> TicketDTO:
    return TicketDTO(
        uid=uid,
        repository_uid="repo1",
        title=title,
        status=TicketStatus(status),
        description=description,
    )


def test_intent_lists_every_candidate_with_uid_and_title():
    tickets = [_ticket("aaa111", title="Fix auth bug"), _ticket("bbb222", title="Harden auth flow")]
    intent = _build_epic_proposal_intent(tickets, "repo1")
    assert "aaa111" in intent and "Fix auth bug" in intent
    assert "bbb222" in intent and "Harden auth flow" in intent
    assert "Repository uid: repo1" in intent
    assert "opensweep_platform_propose_epic" in intent


def test_intent_truncates_long_descriptions_and_flattens_newlines():
    long_desc = "line1\nline2 " + "x" * 500
    intent = _build_epic_proposal_intent([_ticket("a", description=long_desc), _ticket("b")], "r")
    assert "\nline2" not in intent  # newlines flattened inside the excerpt
    assert "…" in intent
    assert "x" * 300 not in intent  # truncated well below the raw length


def test_intent_is_read_only_and_human_gated():
    intent = _build_epic_proposal_intent([_ticket("a"), _ticket("b")], "r")
    assert "do not modify any code" in intent
    assert "human reviews every proposal" in intent


def test_intent_narrows_the_agent_to_root_cause():
    """The six computable axes were the reason grouping under-produced: the
    model was asked to eyeball what set arithmetic answers exactly. It must
    now be told to stay off them."""
    intent = _build_epic_proposal_intent([_ticket("a"), _ticket("b")], "r")
    assert "ROOT CAUSE" in intent
    assert "axis='root-cause'" in intent
    # Explicitly warned off the axes the platform computes itself.
    for computed in ("shared files", "shared subsystem", "shared area", "shared theme"):
        assert computed in intent, f"prompt does not warn off {computed!r}"


def test_intent_asks_for_structured_evidence_and_a_capped_rationale():
    intent = _build_epic_proposal_intent([_ticket("a"), _ticket("b")], "r")
    assert "`evidence`" in intent
    assert str(MAX_RATIONALE_CHARS) in intent
    assert "ONE sentence" in intent


def test_aggressiveness_dial_changes_the_stance_not_the_task():
    intents = {
        level: _build_epic_proposal_intent([_ticket("a"), _ticket("b")], "r", level)
        for level in GROUPING_AGGRESSIVENESS
    }
    # The old hardcoded stance survives only under `conservative`.
    assert "a wrong grouping is worse than no grouping" in intents["conservative"].lower()
    assert "a wrong grouping is worse than no grouping" not in intents["balanced"].lower()
    # Epic ceilings widen monotonically.
    assert "at most 3 epics" in intents["conservative"].lower()
    assert "at most 6 epics" in intents["balanced"].lower()
    assert "at most 10 epics" in intents["exhaustive"].lower()
    # …but every level still states the task and the human gate.
    for intent in intents.values():
        assert "ROOT CAUSE" in intent
        assert "human reviews every proposal" in intent


def test_unknown_aggressiveness_falls_back_to_balanced():
    """A stored rule carrying a stale level must still dispatch a sane run
    rather than 500 or silently emit a prompt with no stance at all."""
    fallback = _build_epic_proposal_intent([_ticket("a"), _ticket("b")], "r", "nonsense")
    balanced = _build_epic_proposal_intent([_ticket("a"), _ticket("b")], "r", "balanced")
    assert fallback == balanced


def test_proposal_to_dto_maps_epic_fields():
    p = EpicProposal(
        uid="p3",
        repository_uid="r",
        title="t",
        axis="files",
        evidence={"shared_paths": ["a.py"]},
        shape="parallel-runs",
        plan_uid="plan1",
        origin="rule",
    )
    dto = proposal_to_dto(p, member_titles=["First", "Second"])
    assert dto.axis == "files"
    assert dto.evidence == {"shared_paths": ["a.py"]}
    assert dto.shape == "parallel-runs"
    assert dto.plan_uid == "plan1"
    assert dto.origin == "rule"
    # Resolved server-side so the panel stops rendering raw uid fragments for
    # members that happen not to be on the client's loaded board.
    assert dto.member_titles == ["First", "Second"]


def test_proposal_to_dto_maps_all_fields():
    p = EpicProposal(
        uid="p1",
        repository_uid="repo1",
        title="Epic: auth cleanup",
        rationale="same subsystem",
        member_ticket_uids=["a", "b"],
        suggested_labels=["auth"],
        suggested_priority="high",
        status="proposed",
        source_run_uid="run1",
    )
    dto = proposal_to_dto(p)
    assert dto.uid == "p1"
    assert dto.repository_uid == "repo1"
    assert dto.title == "Epic: auth cleanup"
    assert dto.rationale == "same subsystem"
    assert dto.member_ticket_uids == ["a", "b"]
    assert dto.suggested_labels == ["auth"]
    assert dto.suggested_priority == "high"
    assert dto.status == EpicProposalStatus.PROPOSED
    assert dto.source_run_uid == "run1"
    assert dto.created_ticket_uid == ""
    assert dto.reviewed_by == ""


def test_proposal_to_dto_defaults_empty_fields():
    p = EpicProposal(uid="p2", repository_uid="r", title="t")
    dto = proposal_to_dto(p)
    assert dto.status == EpicProposalStatus.PROPOSED
    assert dto.member_ticket_uids == []
    assert dto.suggested_priority == "medium"


def test_group_proposal_statuses():
    assert EPIC_PROPOSAL_STATUSES == {"proposed", "approved", "rejected"}
