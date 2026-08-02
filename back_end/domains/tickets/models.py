"""Ticket — unit of plannable work (PLATFORM_V2_DESIGN.md §3, §15 Phase 2).

Replaces the Linear issue. Gate-1 is the backlog → todo transition: nothing
implements without a human (maintainer+) approving. Findings link in via
`linked_finding_uids` (defer/promote), PRs via `linked_pr_uids`; a merged
linked PR completes the ticket (done via merge).
"""

from neomodel import (
    AsyncStructuredNode,
    BooleanProperty,
    DateTimeProperty,
    JSONProperty,
    StringProperty,
)


class Ticket(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    repository_uid = StringProperty(required=True, index=True)

    title = StringProperty(required=True)
    description = StringProperty(default="")
    acceptance_criteria = JSONProperty(default=[])
    labels = JSONProperty(default=[])

    status = StringProperty(default="backlog", index=True)
    # backlog | todo | in-progress | in-review | done  (Gate 1 = backlog → todo)
    priority = StringProperty(default="medium")  # low | medium | high | urgent
    size = StringProperty(default="")  # trivial | small | medium | large

    # Denormalized from the origin Finding at promotion time. NOT the same axis
    # as `priority`: severity is how bad the found thing is (tops out at
    # `critical`), priority is how soon we want it fixed (tops out at `urgent`).
    # Epic selection needs "every critical and high" — a question about the
    # finding — and joining back through origin_finding_uid on every list query
    # would not scale. `kind`/`tags`/`subtype` are here for the same reason:
    # they are the `lens` and `class` epic axes and are unreachable from
    # the Ticket alone.
    severity = StringProperty(default="", index=True)  # low|medium|high|critical
    kind = StringProperty(default="", index=True)
    tags = JSONProperty(default=[])
    subtype = StringProperty(default="")

    origin = StringProperty(default="human", index=True)  # finding | human | agent-proposal
    origin_finding_uid = StringProperty(default="", index=True)
    parent_ticket_uid = StringProperty(default="", index=True)

    # Cross-links to the discovery loop (findings) and delivery loop (PRs).
    linked_finding_uids = JSONProperty(default=[])
    linked_pr_uids = JSONProperty(default=[])

    assignee_uid = StringProperty(default="", index=True)

    # Archived tickets keep their history but leave every default listing —
    # the reversible alternative to delete, which stays backlog-only. Any
    # status can archive; an active thread blocks it (409).
    archived = BooleanProperty(default=False, index=True)
    archived_at = DateTimeProperty()
    archived_by = StringProperty(default="")

    # Implementation plan, written by the ticket's Thread (unified dev flow):
    # {markdown, state (drafted|approved), thread_uid, updated_at,
    #  approved_by, approved_at}. Empty dict = no plan yet.
    plan = JSONProperty(default={})

    # Per-ticket question-policy override: interrogate|assume|strict.
    # "" = inherit the repository default. Exists because "this one is a
    # known-shape chore, don't interrogate me" is the judgment a repo-wide
    # default cannot make.
    autonomy = StringProperty(default="")

    # Assumptions the agent made INSTEAD of asking, under autonomy=assume.
    # [{assumption, because, confidence, result, note, question,
    #   source_run_uid, ts}] — the `result` triple mirrors Verdict.ac_results
    # so the review agent emits a familiar shape. Empty list = none recorded.
    assumptions = JSONProperty(default=[])

    # Gate-1 provenance — set on backlog → todo, kept as the approval record.
    approved_by = StringProperty(default="")
    approved_at = DateTimeProperty()

    done_at = DateTimeProperty()

    created_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)


class EpicProposal(AsyncStructuredNode):
    """An agent-proposed epic of related tickets (PLATFORM_V2_DESIGN.md §15).

    Agents may only PROPOSE groupings — approval is human-only, mirroring
    Gate 1. Approving creates a parent Ticket (origin agent-proposal, born in
    backlog) and re-parents the members under it; rejecting just records the
    verdict. The member tickets themselves are never touched by a proposal.
    """

    uid = StringProperty(unique_index=True, required=True)
    repository_uid = StringProperty(required=True, index=True)

    title = StringProperty(required=True)  # title for the parent ticket on approval
    rationale = StringProperty(default="")  # why these belong in one epic
    member_ticket_uids = JSONProperty(default=[])
    suggested_labels = JSONProperty(default=[])
    suggested_priority = StringProperty(default="medium")

    # What makes these belong together (EpicAxis). Six of the nine proposable
    # axes are computed, not judged — the agent is needed only for
    # `root-cause`, `theme` and `co-change`. Recording the
    # axis is what lets the review card show a one-line structured reason
    # instead of a paragraph of model prose.
    axis = StringProperty(default="root-cause", index=True)
    # Structured, axis-specific support for the claim: shared paths, the area
    # key, the finding class. The UI renders ONE line from this.
    evidence = JSONProperty(default={})
    # Groups epics produced by one selection so they can be approved in bulk.
    plan_uid = StringProperty(default="", index=True)
    # manual | rule | agent — which of the three producers built it.
    origin = StringProperty(default="agent", index=True)

    status = StringProperty(default="proposed", index=True)  # proposed | approved | rejected
    source_run_uid = StringProperty(default="", index=True)  # run that proposed it


    # Review record — set on approve/reject; created_ticket_uid is the parent
    # ticket materialized by approval.
    created_ticket_uid = StringProperty(default="")
    reviewed_by = StringProperty(default="")
    reviewed_at = DateTimeProperty()

    created_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)


TICKET_STATUSES = {"backlog", "todo", "in-progress", "in-review", "done"}

TICKET_ORIGINS = {"finding", "human", "agent-proposal"}

TICKET_PRIORITIES = {"low", "medium", "high", "urgent"}

EPIC_PROPOSAL_STATUSES = {"proposed", "approved", "rejected"}
