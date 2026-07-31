"""Dashboard metric DTOs."""

from datetime import datetime

from pydantic import BaseModel, Field


class FindingStatusCount(BaseModel):
    status: str
    count: int


class FindingTagCount(BaseModel):
    tag: str
    count: int


class RepoSummary(BaseModel):
    repository_uid: str
    repository_name: str
    repository_slug: str
    docs: int = 0
    open_findings: int = 0
    high_severity_findings: int = 0
    proposals: int = 0
    runs_last_24h: int = 0


class AttentionItem(BaseModel):
    """One thing a human should act on. Per-entity items carry a subject_uid;
    aggregate items (count > 1 possible) point at a list surface instead."""

    kind: str
    priority: int  # 1 = most urgent; fixed per kind (attention_service.KIND_PRIORITY)
    title: str
    description: str = ""
    subject_type: str  # Repository | PullRequest | Thread | Run | AreaEdit | EpicProposal | Finding | Area
    subject_uid: str = ""  # "" on aggregate items
    count: int = 1
    created_at: datetime | None = None


class RepoAttention(BaseModel):
    repository_uid: str
    generated_at: datetime
    total: int = 0
    counts_by_kind: dict[str, int] = Field(default_factory=dict)
    items: list[AttentionItem] = Field(default_factory=list)


class OverviewMetrics(BaseModel):
    repositories_github: int = 0
    total_docs: int = 0
    open_findings: int = 0
    high_severity_findings: int = 0
    proposals: int = 0
    runs_last_24h: int = 0
    finding_statuses: list[FindingStatusCount] = Field(default_factory=list)
    finding_tags: list[FindingTagCount] = Field(default_factory=list)
    repositories: list[RepoSummary] = Field(default_factory=list)
