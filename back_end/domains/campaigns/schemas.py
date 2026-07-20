"""Campaign DTOs."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CampaignDTO(BaseModel):
    uid: str
    repository_uid: str
    title: str = ""
    status: str = "planning"
    template: str = "rotation"
    effort: str = ""
    lens_keys: list[str] = Field(default_factory=list)
    # Rotation only: how many areas each pass covers.
    k: int = 3
    # Part dicts as stored: {idx, kind, title, scope_paths, doc_uids,
    # lens_keys, run_uid, state, file_count}.
    parts: list[dict] = Field(default_factory=list)
    max_parallel: int = 2
    created_by: str = ""
    trigger_provenance: str = ""
    summary: dict = Field(default_factory=dict)
    events: list[dict] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CreateCampaignRequest(BaseModel):
    template: str = "rotation"
    # Empty = every enabled lens; "focused" reads its focus lens from the
    # first entry.
    lens_keys: list[str] = Field(default_factory=list)
    # "" = default tiers (areas normal, global sweeps deep).
    effort: str = ""
    # Rotation only: how many areas this pass covers.
    k: int = 3
    max_parallel: int = 2
    title: str = ""


class CampaignAreaPreview(BaseModel):
    """One area of the would-be partition — planner.normalize_areas output."""

    title: str = ""
    scope_paths: list[str] = Field(default_factory=list)
    doc_uids: list[str] = Field(default_factory=list)
    file_count: Optional[int] = None


class CampaignAreasPreview(BaseModel):
    """The partition a campaign would use, computed live, never persisted
    (GET /repositories/{uid}/campaign-areas)."""

    areas: list[CampaignAreaPreview] = Field(default_factory=list)
    # "" = planned against the full tree; else why sizing degraded.
    degraded: str = ""
    total_files: int = 0
    # The remainder ("Uncovered paths") file count — 0 when docs cover everything.
    uncovered_files: int = 0
