"""Area + AreaEdit DTOs."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class AreaEditStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class AreaDTO(BaseModel):
    uid: str
    repository_uid: str
    key: str
    kind: str = "subsystem"
    title: str = ""
    scope_paths: list[str] = []
    spec: str = ""
    doc_uids: list[str] = []
    enabled: bool = True
    provenance: str = "system"
    # Derived: code changed under scope_paths since last review.
    stale: bool = False
    stale_paths: list[str] = []
    code_changed_at: datetime | None = None
    last_reviewed_at: datetime | None = None
    pending_edits: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AreaEditDTO(BaseModel):
    uid: str
    repository_uid: str
    area_uid: str = ""
    key: str = ""
    kind: str = ""
    title: str = ""
    scope_paths: list[str] = []
    doc_uids: list[str] = []
    proposed_spec: str = ""
    proposed_enabled: bool = True
    rationale: str = ""
    # Partition warnings this edit would create — shown in the review queue
    # before accept (advisory, never a blocker).
    warnings: list[str] = []
    source_run_uid: str = ""
    status: AreaEditStatus = AreaEditStatus.PENDING
    resolved_by: str = ""
    resolved_at: datetime | None = None
    created_at: datetime | None = None
    # Current spec of the target area, so the UI can render a diff without a
    # second fetch. Empty for new-area proposals.
    current_spec: str = ""


class UpdateAreaRequest(BaseModel):
    title: str | None = None
    kind: str | None = None
    scope_paths: list[str] | None = None
    spec: str | None = None
    doc_uids: list[str] | None = None
    enabled: bool | None = None


class BulkAreaEditRequest(BaseModel):
    uids: list[str]


class AcceptAreaEditResponse(BaseModel):
    """`area` is null when the edit retired an area that was never mapped —
    accepting "this should not exist" resolves the edit and creates nothing."""

    area: AreaDTO | None = None
    warnings: list[str]


class UpdateAreaResponse(BaseModel):
    """PATCH result: the updated area plus the partition warnings the new
    values create — the same eyeball a human gets at accept time."""

    area: AreaDTO
    warnings: list[str] = Field(default_factory=list)


# ---------- Area detail (GET /areas/{uid}/detail) ----------


class AreaScopeEntryDTO(BaseModel):
    """One scope path sized against the live tree. file_count is None (and
    dead stays False) when the tree is unavailable; files list is capped."""

    path: str
    file_count: int | None = None
    dead: bool = False
    files: list[str] = Field(default_factory=list)


class AreaDocRefDTO(BaseModel):
    uid: str
    slug: str = ""
    title: str = ""


class RelatedAreaDTO(BaseModel):
    uid: str
    key: str
    kind: str = "subsystem"
    title: str = ""


class SubFeatureDTO(BaseModel):
    """A sub-feature leaf under a parent feature grouping, with its own
    staleness + coverage count — rendered as a child row in the feature
    tree. `is_leaf` is always True here (only leaves are audit targets)."""

    uid: str
    key: str
    title: str = ""
    spec: str = ""
    stale: bool = False
    has_spec: bool = False
    coverage_count: int = 0


class AreaCoverageDTO(BaseModel):
    """One Checked stamp whose covered paths overlap this area's scope."""

    run_uid: str
    outcome: str = ""
    checked_at: datetime | None = None
    lens_verdicts: list[dict] = Field(default_factory=list)
    # reported | inferred | unknown — "reported" is the only value backed by the
    # agent's own coverage contract. The other two mean the paths are the run's
    # dispatched scope, so this row says where a run was aimed, not what it read.
    coverage_source: str = "unknown"


class AreaHealthRowDTO(BaseModel):
    """One row of the merged Areas board: the area, plus every upkeep signal
    that used to live on the separate Health page.

    Three independent axes, deliberately not collapsed into one score:
    REVIEW (`stale` — code moved under the scope since the map was last
    verified), DOCS (how many covering pages are themselves stale), and AUDIT
    COVERAGE (`last_checked`/`outcome` — whether anything has looked at this
    code, which a stale map does not tell you and vice versa).
    """

    uid: str
    key: str
    kind: str = "subsystem"
    title: str = ""
    enabled: bool = True
    # Files belong to leaves; non-leaf rows are pure groupings and roll their
    # children up rather than owning scope of their own.
    is_leaf: bool = True
    depth: int = 0
    scope_paths: list[str] = Field(default_factory=list)
    # None when the file tree was unavailable — never guess a count.
    file_count: int | None = None

    # Review axis
    stale: bool = False
    stale_paths: list[str] = Field(default_factory=list)
    # Stale leaves under this key. Lets a grouping row show that its children
    # need review even though a grouping owns no paths and can never itself
    # be stale.
    stale_descendants: int = 0
    code_changed_at: datetime | None = None
    last_reviewed_at: datetime | None = None
    pending_edits: int = 0

    # Docs axis
    docs_total: int = 0
    docs_stale: int = 0

    # Spec axis: present | missing | stale. "stale" mirrors the rule
    # generate-specs already applies (a spec'd leaf whose code moved).
    spec_state: str = "present"

    # Audit-coverage axis — the latest overlapping Checked stamp.
    last_checked: datetime | None = None
    outcome: str = ""
    revision: str = ""
    # reported | inferred | unknown for THAT stamp. Non-"reported" means the
    # covered paths are the run's dispatched scope, so `last_checked` records
    # that a run was aimed here — not that it read this. The summary tiles
    # deliberately still count it as coverage (see area_health).
    coverage_source: str = "unknown"


class UnassignedDocDTO(BaseModel):
    """A doc page no area covers — it would otherwise vanish when the Health
    page folds into Areas."""

    uid: str
    slug: str = ""
    title: str = ""
    stale: bool = False
    last_checked: datetime | None = None
    outcome: str = ""


class AreaHealthSummaryDTO(BaseModel):
    """The Health page's stat tiles, recomputed over auditable area leaves
    (groupings are excluded — they own no files, so counting them would
    inflate every tile)."""

    total: int = 0
    stale: int = 0
    never_audited: int = 0
    fresh: int = 0


class AreasHealthDTO(BaseModel):
    repository_uid: str
    rows: list[AreaHealthRowDTO] = Field(default_factory=list)
    unassigned_docs: list[UnassignedDocDTO] = Field(default_factory=list)
    summary: AreaHealthSummaryDTO = Field(default_factory=AreaHealthSummaryDTO)
    # "" = file counts sized against the full tree; else why they degraded.
    tree_degraded: str = ""
    # Freshness cursor honesty: when the push→stale path last completed, and
    # why it is partial if it is. A board that cannot see recent commits must
    # say so rather than render a confident all-fresh green.
    freshness_synced_at: datetime | None = None
    freshness_degraded_reason: str = ""


class AreaDetailDTO(BaseModel):
    area: AreaDTO
    scope: list[AreaScopeEntryDTO] = Field(default_factory=list)
    # "" = sized against the full tree; else why the tree was unavailable.
    tree_degraded: str = ""
    # Docs related to this area — the agent-proposed doc_uids plus every
    # page whose watch_paths overlap the scope. Informational, not curated:
    # audit runs get the same set as likely-relevant leads at dispatch.
    related_docs: list[AreaDocRefDTO] = Field(default_factory=list)
    # Feature → intersecting subsystem leaves; subsystem → features
    # referencing it.
    related_areas: list[RelatedAreaDTO] = Field(default_factory=list)
    # Last 10 overlapping Checked stamps, newest first. For a PARENT feature
    # this is the aggregated (rolled-up) coverage across its sub-feature
    # leaves; a sub-feature (or any non-feature area) shows its own.
    coverage: list[AreaCoverageDTO] = Field(default_factory=list)
    pending_edits: list[AreaEditDTO] = Field(default_factory=list)
    # Sub-feature leaves under a PARENT feature grouping (empty otherwise) —
    # the feature tree's child rows; the parent's `coverage` above is their
    # rollup. `is_feature_parent` flags that this area is a grouping, so its
    # spec is a charter (not an audit target).
    sub_features: list[SubFeatureDTO] = Field(default_factory=list)
    is_feature_parent: bool = False
