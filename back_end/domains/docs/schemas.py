"""Doc + DocEdit DTOs."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class DocEditStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class DocDTO(BaseModel):
    uid: str
    repository_uid: str
    slug: str
    title: str = ""
    summary: str = ""
    body: str = ""
    pinned: bool = False
    # Retired page: excluded from listings/briefing/export/audit selection.
    archived: bool = False
    watch_paths: list[str] = []
    # Derived: code changed under watch_paths since last review.
    # False = no watch_paths at all: the webhook has nothing to match, so
    # `stale` can never become True. That is untracked, not current.
    tracked: bool = True
    stale: bool = False
    stale_paths: list[str] = []
    code_changed_at: datetime | None = None
    last_reviewed_at: datetime | None = None
    pending_edits: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DocEditDTO(BaseModel):
    uid: str
    repository_uid: str
    doc_uid: str = ""
    slug: str = ""
    title: str = ""
    summary: str = ""
    watch_paths: list[str] = []
    proposed_body: str = ""
    rationale: str = ""
    # True = the edit proposes RETIRING the target page (applied on accept).
    proposed_archived: bool = False
    source_run_uid: str = ""
    status: DocEditStatus = DocEditStatus.PENDING
    resolved_by: str = ""
    resolved_at: datetime | None = None
    created_at: datetime | None = None
    # Current body of the target doc, so the UI can render a diff without a
    # second fetch. Empty for new-page proposals.
    current_body: str = ""


class CreateDocRequest(BaseModel):
    repository_uid: str
    slug: str
    title: str = ""
    summary: str = ""
    body: str = ""
    watch_paths: list[str] = []
    pinned: bool = False


class UpdateDocRequest(BaseModel):
    title: str | None = None
    summary: str | None = None
    body: str | None = None
    watch_paths: list[str] | None = None


class SetPinnedRequest(BaseModel):
    pinned: bool


class BulkEditRequest(BaseModel):
    uids: list[str]
