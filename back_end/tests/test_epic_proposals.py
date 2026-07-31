"""Pure-function tests for ticket grouping — intent builder + DTO mapping."""

import pytest

from api.v1.tickets import (
    GROUPING_AGGRESSIVENESS,
    GROUPING_GOALS,
    _build_epic_proposal_intent,
    computed_clusters,
)
from domains.tickets.models import EPIC_PROPOSAL_STATUSES, EpicProposal
from domains.tickets.schemas import EpicProposalStatus
from domains.tickets.services.epic_service import proposal_to_dto
from domains.tickets.services.epics.schemas import (
    AGENT_AXES,
    MAX_RATIONALE_CHARS,
    TicketFacts,
)


def _ticket(
    uid: str,
    *,
    title: str = "t",
    status: str = "backlog",
    description: str = "",
    paths: tuple[str, ...] = (),
    area_keys: tuple[str, ...] = (),
) -> TicketFacts:
    return TicketFacts(
        uid=uid,
        title=title,
        description=description,
        status=status,
        paths=paths,
        area_keys=area_keys,
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


def test_intent_asks_for_structured_evidence_and_a_capped_rationale():
    intent = _build_epic_proposal_intent([_ticket("a"), _ticket("b")], "r")
    assert "`evidence`" in intent
    assert str(MAX_RATIONALE_CHARS) in intent
    assert "ONE sentence" in intent


# ── goals ──────────────────────────────────────────────────────────────────


def test_default_goal_is_root_cause_and_keeps_the_computed_axis_guard_rail():
    """With root cause as the only ground, the model must still be warned off
    the six axes the platform computes exactly — proposing one of those is a
    review a human pays for a grouping they could have had for free."""
    intent = _build_epic_proposal_intent([_ticket("a"), _ticket("b")], "r")
    assert "ROOT CAUSE" in intent
    assert "'root-cause'" in intent
    for computed in ("shared files", "shared subsystem", "shared area", "shared theme"):
        assert computed in intent, f"prompt does not warn off {computed!r}"


def test_theme_and_co_change_goals_lift_the_root_cause_guard_rail():
    """The guard rail forbids grouping on a shared theme. Left in place next
    to a theme goal it contradicts the task — the run would be told to do and
    not do the same thing."""
    intent = _build_epic_proposal_intent(
        [_ticket("a"), _ticket("b")], "r", goals=["root-cause", "theme", "co-change"]
    )
    assert "Do not group on shared files" not in intent
    assert "THEME" in intent and "'theme'" in intent
    assert "CO-CHANGE" in intent and "'co-change'" in intent
    # Root cause is still asked for — adding grounds must not replace them.
    assert "ROOT CAUSE" in intent


@pytest.mark.parametrize("goal", sorted(GROUPING_GOALS))
def test_every_goal_names_the_axis_the_proposal_must_carry(goal: str):
    """A run asked for several grounds returns several kinds of claim, and the
    reviewer can only tell them apart by the axis stamped on each."""
    intent = _build_epic_proposal_intent([_ticket("a"), _ticket("b")], "r", goals=[goal])
    assert f"'{goal}'" in intent


def test_unknown_goal_is_dropped_rather_than_emptying_the_task():
    """A saved rule carrying a goal from a later version must still dispatch a
    run with something to do, not a prompt with no grounds at all."""
    only_bad = _build_epic_proposal_intent([_ticket("a"), _ticket("b")], "r", goals=["nonsense"])
    default = _build_epic_proposal_intent([_ticket("a"), _ticket("b")], "r")
    assert only_bad == default

    mixed = _build_epic_proposal_intent(
        [_ticket("a"), _ticket("b")], "r", goals=["nonsense", "theme"]
    )
    assert "THEME" in mixed


def test_goals_are_deduped_and_hold_their_order():
    intent = _build_epic_proposal_intent(
        [_ticket("a"), _ticket("b")], "r", goals=["theme", "theme", "root-cause"]
    )
    assert intent.count("**THEME**") == 1
    assert intent.index("**THEME**") < intent.index("**ROOT CAUSE**")


# ── aggressiveness ─────────────────────────────────────────────────────────


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


def test_the_epic_ceiling_is_a_total_not_a_per_goal_budget():
    """"At most 6 epics" next to three grounds otherwise reads as eighteen."""
    intent = _build_epic_proposal_intent(
        [_ticket("a"), _ticket("b")], "r", goals=sorted(GROUPING_GOALS)
    )
    assert "total across every ground" in intent


def test_unknown_aggressiveness_falls_back_to_balanced():
    """A stored rule carrying a stale level must still dispatch a sane run
    rather than 500 or silently emit a prompt with no stance at all."""
    fallback = _build_epic_proposal_intent([_ticket("a"), _ticket("b")], "r", "nonsense")
    balanced = _build_epic_proposal_intent([_ticket("a"), _ticket("b")], "r", "balanced")
    assert fallback == balanced


# ── computed clusters as prior art ─────────────────────────────────────────


def test_computed_clusters_are_shown_with_members_and_a_do_not_repeat_rule():
    facts = [
        _ticket("a", paths=("svc.py",)),
        _ticket("b", paths=("svc.py",)),
        _ticket("c"),
    ]
    clusters = computed_clusters(facts)
    assert clusters, "two tickets sharing a file must form a computed cluster"

    intent = _build_epic_proposal_intent(facts, "r", clusters=clusters)
    assert "already computed" in intent
    assert "a, b" in intent  # the cluster's members, so the agent can extend it
    assert "do not propose it" in intent
    assert "`evidence.extends`" in intent


def test_pathless_candidates_are_flagged_as_invisible_to_the_computed_axes():
    """The population theme/co-change exist for. A ticket with no findings
    holds no paths and therefore no area key, so it sits in no computed
    cluster however plainly it belongs — the prompt has to say so, or the
    agent reads its absence as 'already handled'."""
    intent = _build_epic_proposal_intent([_ticket("a"), _ticket("b", paths=("x.py",))], "r")
    assert "invisible to every computed axis" in intent


def test_intent_carries_the_facets_the_computed_axes_keyed_on():
    """A theme claim is groundable only if the model can see what arithmetic
    already saw. Titles alone were never enough."""
    intent = _build_epic_proposal_intent(
        [
            _ticket("a", paths=("back_end/a.py",), area_keys=("back_end/domains/areas",)),
            _ticket("b"),
        ],
        "r",
    )
    assert "back_end/domains/areas" in intent
    assert "back_end/a.py" in intent


def test_no_computed_clusters_says_so_rather_than_showing_an_empty_list():
    intent = _build_epic_proposal_intent([_ticket("a"), _ticket("b")], "r", clusters=[])
    assert "None:" in intent
    assert "Everything here is yours to find" in intent


# ── the agent axes are the only ones an agent may claim ────────────────────


def test_every_goal_maps_to_an_agent_axis():
    """A goal whose axis the platform computes would make the run propose
    what the rule builder produces for free. `partition` refusing those axes
    (see test_epic_planning) is the other half of this."""
    assert GROUPING_GOALS == {a.value for a in AGENT_AXES}


def test_proposal_to_dto_maps_epic_fields():
    p = EpicProposal(
        uid="p3",
        repository_uid="r",
        title="t",
        axis="files",
        evidence={"shared_paths": ["a.py"]},
        plan_uid="plan1",
        origin="rule",
    )
    dto = proposal_to_dto(p, member_titles=["First", "Second"])
    assert dto.axis == "files"
    assert dto.evidence == {"shared_paths": ["a.py"]}
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
