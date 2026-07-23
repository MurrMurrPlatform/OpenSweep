"""Campaign DTOs."""

from datetime import datetime

from pydantic import BaseModel, Field


class CampaignDTO(BaseModel):
    uid: str
    repository_uid: str
    title: str = ""
    status: str = "planning"
    template: str = "rotation"
    # subsystem | feature | global | batch
    kind: str = "subsystem"
    # all | stale | rotation
    selection: str = "all"
    # Area keys this campaign covers (empty = whole map)
    coverage_keys: list[str] = Field(default_factory=list)
    # Batch wiring
    parent_uid: str = ""
    child_uids: list[str] = Field(default_factory=list)
    effort: str = ""
    lens_keys: list[str] = Field(default_factory=list)
    # Rotation only: how many areas each pass covers.
    k: int = 3
    # Area-map campaigns only: the plan is sliced to areas at or under this
    # key ("" = the whole map).
    area_prefix: str = ""
    # Part dicts as stored: {idx, kind, title, scope_paths, doc_uids,
    # lens_keys, run_uid, state, file_count, area_keys}.
    parts: list[dict] = Field(default_factory=list)
    max_parallel: int = 2
    created_by: str = ""
    trigger_provenance: str = ""
    summary: dict = Field(default_factory=dict)
    # How the planner reconciled the area map into `parts`: {source,
    # map_areas, leaves, groupings, features, ignored, area_parts,
    # bundled_leaves, feature_parts, global_parts, oversized, degraded,
    # area_prefix}. {} on campaigns planned before this field existed.
    plan_summary: dict = Field(default_factory=dict)
    events: list[dict] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CreateCampaignRequest(BaseModel):
    # "" = legacy template path; "subsystem"|"feature"|"global"|"batch" = new kind path.
    kind: str = ""
    # Which areas/files to target: "" = default; "all"|"stale"|"rotation".
    selection: str = ""
    # Area keys to restrict to (empty = whole map).
    coverage_keys: list[str] = Field(default_factory=list)
    template: str = "rotation"
    # Empty = every enabled lens; "focused" reads its focus lens from the
    # first entry.
    lens_keys: list[str] = Field(default_factory=list)
    # "" = default tiers (areas normal, global sweeps deep).
    effort: str = ""
    # Rotation only: how many areas this pass covers.
    k: int = 3
    # Area-map campaigns only: plan just the areas at or under this key
    # ("" = the whole map; ignored for docs-derived plans).
    area_prefix: str = ""
    max_parallel: int = 2
    title: str = ""


class CampaignAreaPreview(BaseModel):
    """One area of the would-be partition — planner.normalize_areas or
    planner.areas_from_map output."""

    title: str = ""
    scope_paths: list[str] = Field(default_factory=list)
    doc_uids: list[str] = Field(default_factory=list)
    file_count: int | None = None
    # Area-map source only: the Area's key ("" for docs-derived/remainder).
    area_key: str = ""
    # "subsystem" tiles the tree; "feature" overlays it (implementation-gaps).
    kind: str = "subsystem"
    # Map leaves above the size target are flagged, never auto-split —
    # resizing them is the mapping agent's job.
    oversized: bool = False
    # Scope entries matching zero tree files — likely typos or moved code.
    # Always [] for docs-derived areas and when the tree is unavailable.
    dead_scope_paths: list[str] = Field(default_factory=list)


class CampaignAreasPreview(BaseModel):
    """The partition a campaign would use, computed live, never persisted
    (GET /repositories/{uid}/campaign-areas)."""

    areas: list[CampaignAreaPreview] = Field(default_factory=list)
    # "" = planned against the full tree; else why sizing degraded.
    degraded: str = ""
    total_files: int = 0
    # The remainder ("Uncovered paths") file count — 0 when docs cover everything.
    uncovered_files: int = 0
    # "area-map" when the enabled Area map planned this; "docs" fallback.
    source: str = "docs"
    # Titles of oversized map leaves — the "refine your map" nudge.
    oversized_areas: list[str] = Field(default_factory=list)
    # Area-map partition health: files claimed by more than one subsystem
    # leaf, and ignore scope entries matching no tree files. 0/[] for
    # docs-derived partitions and when the tree is unavailable.
    overlapping_files: int = 0
    dead_ignore_scopes: list[str] = Field(default_factory=list)
