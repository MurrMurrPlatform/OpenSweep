"""Area + AreaEdit DTOs."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


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
