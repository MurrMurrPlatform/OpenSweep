"""Ticket domain schemas — DTOs, enums, requests (PLATFORM_V2_DESIGN.md §3)."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class TicketStatus(str, Enum):
    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in-progress"
    IN_REVIEW = "in-review"
    DONE = "done"


class TicketDTO(BaseModel):
    uid: str
    repository_uid: str
    title: str
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    status: TicketStatus = TicketStatus.BACKLOG
    priority: str = "medium"
    size: str = ""
    # Denormalized from the origin finding — the epic axes. See
    # Ticket.severity in models.py for why these are not joined on demand.
    severity: str = ""
    kind: str = ""
    tags: list[str] = Field(default_factory=list)
    subtype: str = ""
    origin: str = "human"
    origin_finding_uid: str = ""
    parent_ticket_uid: str = ""
    linked_finding_uids: list[str] = Field(default_factory=list)
    linked_pr_uids: list[str] = Field(default_factory=list)
    assignee_uid: str = ""
    # Thread-authored implementation plan metadata (unified dev flow).
    plan: dict = Field(default_factory=dict)
    approved_by: str = ""
    approved_at: datetime | None = None
    # Archived tickets keep history but leave default listings (see models.py).
    archived: bool = False
    archived_at: datetime | None = None
    archived_by: str = ""
    done_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TicketDetailDTO(TicketDTO):
    """Ticket + its subtickets."""

    children: list[TicketDTO] = Field(default_factory=list)


# ── Requests ─────────────────────────────────────────────────────────────────


class CreateTicketRequest(BaseModel):
    title: str = Field(min_length=1)
    repository_uid: str = Field(min_length=1)
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    priority: str = "medium"
    size: str = ""
    origin: str = "human"
    origin_finding_uid: str = ""
    parent_ticket_uid: str = ""
    assignee_uid: str = ""
    # Normally derived from `origin_finding_uid` by the service; accepted
    # explicitly so callers that already hold the finding (bulk promote) can
    # skip the re-read.
    severity: str = ""
    kind: str = ""
    tags: list[str] = Field(default_factory=list)
    subtype: str = ""


class UpdateTicketRequest(BaseModel):
    """Field updates only — status moves exclusively through the transition
    endpoint so every move is legality-checked and audited."""

    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    acceptance_criteria: list[str] | None = None
    labels: list[str] | None = None
    priority: str | None = None
    size: str | None = None
    parent_ticket_uid: str | None = None
    assignee_uid: str | None = None


class TransitionTicketRequest(BaseModel):
    status: TicketStatus


class LinkFindingRequest(BaseModel):
    finding_uid: str = Field(min_length=1)


class LinkPullRequestRequest(BaseModel):
    pull_request_uid: str = Field(min_length=1)


# ── Grouping (parent/subtickets as one implementable epic) ──────────────────


class CreateEpicRequest(BaseModel):
    """Group ≥2 existing tickets under a new parent ticket."""

    repository_uid: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = ""
    member_ticket_uids: list[str] = Field(min_length=2)
    labels: list[str] = Field(default_factory=list)
    priority: str = "medium"


class EpicProposalStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class EpicProposalDTO(BaseModel):
    uid: str
    repository_uid: str
    title: str
    rationale: str = ""
    member_ticket_uids: list[str] = Field(default_factory=list)
    #: Member titles resolved server-side, parallel to `member_ticket_uids`.
    #: The review panel used to look these up against whatever tickets the
    #: board happened to have loaded, so a filtered-out member rendered as a
    #: raw uid fragment.
    member_titles: list[str] = Field(default_factory=list)
    suggested_labels: list[str] = Field(default_factory=list)
    suggested_priority: str = "medium"
    #: What makes these belong together, and the structured support for it —
    #: this is what the card renders as a one-line reason.
    axis: str = "root-cause"
    evidence: dict = Field(default_factory=dict)
    plan_uid: str = ""
    origin: str = "agent"
    status: EpicProposalStatus = EpicProposalStatus.PROPOSED
    source_run_uid: str = ""
    created_ticket_uid: str = ""
    reviewed_by: str = ""
    reviewed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── Epic building (deterministic axes — no agent involved) ──────────────────


class TriageBatchRequest(BaseModel):
    """Triage N tickets in ONE run instead of N runs.

    Same selection vocabulary as `PlanEpicsRequest` / `SuggestEpicsRequest`,
    because all three narrow the same pool through `select_tickets`.
    """

    repository_uid: str = Field(min_length=1)
    statuses: list[str] = Field(default_factory=lambda: ["backlog", "todo"])
    min_priority: str = ""
    min_severity: str = ""
    labels: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)
    area_keys: list[str] = Field(default_factory=list)
    limit: int = Field(default=20, ge=0)
    sort: str = "priority"
    # Defaults to `assume` HERE ONLY. This endpoint has no prior behaviour, so
    # it is not a change to anything; per-ticket refine and thread create keep
    # `interrogate`. Batching exists to spend human attention once, which a
    # per-ticket interrogation would defeat.
    autonomy: str = "assume"
    dry_run: bool = False


class PlanEpicsRequest(BaseModel):
    """Select tickets by rule and cut them into epics on a computed axis.

    This is the path that does NOT need a language model: an area key matches
    or it does not, paths overlap or they do not. It returns proposals
    synchronously instead of dispatching a run.
    """

    repository_uid: str = Field(min_length=1)
    # Selection
    statuses: list[str] = Field(default_factory=lambda: ["backlog", "todo"])
    min_priority: str = ""
    min_severity: str = ""
    labels: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)
    area_keys: list[str] = Field(default_factory=list)
    limit: int = Field(default=20, ge=0)
    sort: str = "priority"
    # Partition — `max_epics` is the "…in Y runs" knob.
    axis: str = "area"
    max_epics: int = Field(default=4, ge=0)
    min_members: int = Field(default=2, ge=2)
    max_members: int = Field(default=6, ge=2)
    #: Preview without persisting anything.
    dry_run: bool = False


class BulkApproveRequest(BaseModel):
    """Approve several proposals in one action (the single bulk gate)."""

    uids: list[str] = Field(min_length=1)

