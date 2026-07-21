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
    area: AreaDTO
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


class AreaCoverageDTO(BaseModel):
    """One Checked stamp whose covered paths overlap this area's scope."""

    run_uid: str
    outcome: str = ""
    checked_at: datetime | None = None
    lens_verdicts: list[dict] = Field(default_factory=list)


class AreaDetailDTO(BaseModel):
    area: AreaDTO
    scope: list[AreaScopeEntryDTO] = Field(default_factory=list)
    # "" = sized against the full tree; else why the tree was unavailable.
    tree_degraded: str = ""
    linked_docs: list[AreaDocRefDTO] = Field(default_factory=list)
    # Docs whose watch_paths overlap the scope but aren't linked yet.
    suggested_docs: list[AreaDocRefDTO] = Field(default_factory=list)
    # Feature → intersecting subsystem leaves; subsystem → features
    # referencing it.
    related_areas: list[RelatedAreaDTO] = Field(default_factory=list)
    # Last 10 overlapping Checked stamps, newest first.
    coverage: list[AreaCoverageDTO] = Field(default_factory=list)
    pending_edits: list[AreaEditDTO] = Field(default_factory=list)
