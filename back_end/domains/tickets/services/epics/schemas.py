"""The epic planning contract: how work is SELECTED, PARTITIONED, and SHAPED.

An epic is a set of tickets one implement run picks up together. Three
producers build them — human multi-select, a deterministic rule, and the
grouping agent — but they all emit `EpicDraft`s through the same two pure
steps, so the three paths cannot drift into three different notions of what an
epic is.

The axis vocabulary is the load-bearing idea here. Six of the seven axes are
ARITHMETIC — a shared file either overlaps or it does not, an area key either
matches or it does not. Only `root-cause` needs a language model. Grouping was
previously ALL agent judgment, which is why it yielded so little: the model was
being asked to eyeball things that `set.intersection` answers exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class EpicAxis(StrEnum):
    """What makes the members of an epic belong together."""

    #: One human ticked the boxes. No claim of relatedness is made.
    MANUAL = "manual"
    #: Members' paths fall under one subsystem area leaf.
    AREA = "area"
    #: Members' paths fall under one feature area.
    FEATURE = "feature"
    #: Members' `affected_paths` overlap (transitively — see `axes.by_files`).
    FILES = "files"
    #: Members share a finding kind/lens (security, test-gaps, …).
    LENS = "lens"
    #: Members share a `tag/subtype` finding class — the ratchet dimension.
    CLASS = "class"
    #: Members are explicitly linked through the finding graph.
    LINKED = "linked"
    #: Members share an underlying defect. Requires agent judgment.
    ROOT_CAUSE = "root-cause"


#: Axes a machine can compute. `ROOT_CAUSE` is deliberately absent: it is the
#: one thing the grouping agent is now asked to do, and the only thing.
DETERMINISTIC_AXES = frozenset(
    {
        EpicAxis.AREA,
        EpicAxis.FEATURE,
        EpicAxis.FILES,
        EpicAxis.LENS,
        EpicAxis.CLASS,
        EpicAxis.LINKED,
    }
)


class EpicShape(StrEnum):
    """How the epic executes once approved."""

    #: One thread, one branch, one PR closing every member.
    SINGLE_PR = "single-pr"
    #: One run per member, fanned out under provider headroom.
    PARALLEL_RUNS = "parallel-runs"


def default_shape_for(axis: EpicAxis) -> EpicShape:
    """Members of an epic are related BY CONSTRUCTION, so they would collide
    on one branch — which is exactly why they belong in one PR rather than
    racing each other. `LENS` is the exception: "all the security tickets"
    spreads across the whole repo with no expected file overlap, so those
    genuinely parallelize.

    Parallelism across epics is where "top X issues in Y runs" comes from —
    Y epics, disjoint by construction, each one run.
    """
    return EpicShape.PARALLEL_RUNS if axis is EpicAxis.LENS else EpicShape.SINGLE_PR


@dataclass(frozen=True)
class EpicSelection:
    """WHICH tickets are eligible, and how many. The `min_*` fields are `>=`
    ladder comparisons (see `infrastructure.ranking`) — the exact-match
    `?severity=` filter on the findings API could never express
    "critical and high", which is the thing people actually ask for.
    """

    statuses: tuple[str, ...] = ("backlog", "todo")
    min_priority: str = ""
    min_severity: str = ""
    labels: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    area_keys: tuple[str, ...] = ()
    # NOTE: excluding already-placed tickets is NOT a selection concern — it
    # needs the parent/child graph, which these flat facts deliberately do not
    # carry. `loader.load_ticket_facts(exclude_grouped=...)` owns it, and a
    # duplicate knob here would let a caller set it and see nothing happen.
    #: "top X" — 0 means no cap.
    limit: int = 20
    #: priority | severity | age | size
    sort: str = "priority"


@dataclass(frozen=True)
class EpicPartition:
    """HOW the selected tickets are cut into epics."""

    axis: EpicAxis = EpicAxis.AREA
    #: "…in Y runs". 0 means no cap on epic count.
    max_epics: int = 4
    #: An epic of one is just a ticket — it buys nothing and costs a review.
    min_members: int = 2
    max_members: int = 6


@dataclass(frozen=True)
class TicketFacts:
    """A ticket plus the finding-derived facts the axes need.

    Tickets do not carry `affected_paths`, `kind`, or `tags` — Findings do,
    and a ticket reaches them through `origin_finding_uid` /
    `linked_finding_uids`. Resolving that is I/O, so a loader does it once and
    hands the partitioner this flat view. That is what keeps every axis a pure
    function over data rather than a service that quietly hits Neo4j per
    candidate (the shape `planner.build_plan_by_kind` already uses).
    """

    uid: str
    title: str = ""
    priority: str = "medium"
    severity: str = ""
    status: str = "backlog"
    labels: tuple[str, ...] = ()
    size: str = ""
    #: Union of affected_paths across the ticket's origin + linked findings.
    paths: tuple[str, ...] = ()
    #: Finding kind (defect/improvement/gap/…) — the lens axis.
    kind: str = ""
    #: Finding tags; with `subtype` these form the `CLASS` axis key.
    tags: tuple[str, ...] = ()
    subtype: str = ""
    #: Every finding uid this ticket touches — the LINKED axis walks these.
    finding_uids: tuple[str, ...] = ()
    #: Area keys whose scope_paths cover `paths`, resolved by the loader.
    area_keys: tuple[str, ...] = ()
    feature_keys: tuple[str, ...] = ()
    updated_at: object = None


@dataclass
class EpicDraft:
    """A proposed epic, before it is persisted or approved.

    `evidence` is what replaces the free-prose rationale that made the review
    card unreadable: structured, machine-produced, and renderable as one line
    (`shared: area_service.py +2`) instead of a paragraph of model output.
    """

    title: str
    member_ticket_uids: list[str]
    axis: EpicAxis
    shape: EpicShape
    evidence: dict = field(default_factory=dict)
    rationale: str = ""
    suggested_priority: str = "medium"
    suggested_labels: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.shape:
            self.shape = default_shape_for(self.axis)


#: Hard ceiling on agent-authored rationale. The grouping tool accepted an
#: unbounded string, and the review card rendered it as full markdown — the
#: verbosity complaint starts here, not in the CSS.
MAX_RATIONALE_CHARS = 240
