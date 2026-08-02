"""Repository DTOs (GitHub-only)."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class RepositoryMode(StrEnum):
    GITHUB = "github"


class RepositoryDTO(BaseModel):
    uid: str
    # Owning organization (tenancy root — domains/tenancy.py).
    org_uid: str
    slug: str
    mode: RepositoryMode
    # Git hosting provider key (infrastructure/git_providers) — additive,
    # "github" is the only value today.
    provider: str = "github"
    name: str
    description: str = ""
    default_branch: str = "main"
    color_scheme: str = "indigo"
    is_active: bool = True

    github_owner: str | None = None
    github_repo: str | None = None
    github_repo_id: int | None = None
    github_installation_id: int | None = None
    # PAT-connection the repo was registered through — credential resolution
    # (infrastructure/github_app.get_repo_git_token) reads it off whatever
    # object it is handed, so the DTO must carry it or every DTO call site
    # (sandbox clones, docs export) silently loses the PAT-connection tier.
    git_connection_uid: str | None = None
    github_connection_status: str | None = None
    last_synced_at: datetime | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)

    # Kill switch — when true, all autonomous + pending Run dispatches for
    # this repo are halted at the API layer (409).
    kill_switch_active: bool = False

    # Agent autonomy — when true, agents may perform otherwise human-only
    # operations for this repo via the platform-tool MCP surface (e.g. ticket
    # status transitions, including Gate-1 approval).
    agent_autonomy: bool = False

    # Freshness honesty. Staleness for BOTH Docs and Areas is marked in one
    # pass off the same cursor, so when that pass cannot see everything the
    # Documentation page is as wrong as the Areas board — it just had no way to
    # say so, because these only ever reached the areas-health response.
    freshness_synced_at: datetime | None = None
    freshness_degraded_reason: str = ""

    created_at: datetime | None = None
    updated_at: datetime | None = None


class PlatformConfigDTO(BaseModel):
    global_kill_switch: bool = False
    updated_at: datetime | None = None


class SetKillSwitchRequest(BaseModel):
    active: bool


class SetAgentAutonomyRequest(BaseModel):
    enabled: bool


class CreateRepositoryRequest(BaseModel):
    slug: str
    mode: RepositoryMode = RepositoryMode.GITHUB
    name: str
    description: str = ""
    default_branch: str = "main"
    color_scheme: str = "indigo"
    github_owner: str
    github_repo: str

    @field_validator("github_owner", "github_repo")
    @classmethod
    def _require_non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("github_owner and github_repo are required (GitHub-only platform)")
        return v


class UpdateRepositoryRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    default_branch: str | None = None
    color_scheme: str | None = None
    is_active: bool | None = None
    github_owner: str | None = None
    github_repo: str | None = None
    agent_autonomy: bool | None = None


# ── GitHub mock DTOs ────────────────────────────────────────────────────────


class GitHubRepoDTO(BaseModel):
    owner: str
    name: str
    full_name: str
    default_branch: str
    url: str
    open_issues_count: int = 0
    pr_count: int = 0


class GitHubIssueDTO(BaseModel):
    number: int
    title: str
    body: str = ""
    state: Literal["open", "closed"]
    author: str
    labels: list[str] = Field(default_factory=list)
    created_at: datetime


class CheckRunDTO(BaseModel):
    name: str
    status: str
    conclusion: str | None = None
    branch: str
    sha: str
    url: str | None = None


class DepAlertDTO(BaseModel):
    package: str
    severity: Literal["low", "medium", "high", "critical"]
    summary: str
    fixed_in: str | None = None


class FileContentDTO(BaseModel):
    path: str
    content: str
    language: str | None = None
    total_lines: int
    start_line: int = 1
    end_line: int
    truncated: bool = False
    source: Literal["github", "missing"]
    bytes_total: int | None = None


# ── Git credential linkage ──────────────────────────────────────────────────


class ConnectionCandidateDTO(BaseModel):
    """A credential in the org this repository could deliver through."""

    kind: Literal["pat", "app"]
    uid: str
    account: str = ""
    installation_id: int | None = None
    is_current: bool = False


class RepoConnectionDTO(BaseModel):
    """Whether this repository can currently deliver work, and what it could
    be linked to instead.

    `state` mirrors `write_gate.WriteAccess` exactly so the banner can never
    claim a repo is fine while dispatch refuses to run it:
      ok           — a credential resolves and nothing says it cannot push
      disconnected — no usable credential at all (the connection was deleted,
                     or none was ever set and GITHUB_TOKEN is unset)
      denied       — a credential resolves but GitHub refuses writes with it
      unknown      — could not be determined; never blocks
    """

    state: Literal["ok", "disconnected", "denied", "unknown"]
    credential: str = ""
    reason: str = ""
    candidates: list[ConnectionCandidateDTO] = Field(default_factory=list)


class LinkConnectionRequest(BaseModel):
    """Point a repository at one of its org's connections (PAT or App)."""

    connection_uid: str
