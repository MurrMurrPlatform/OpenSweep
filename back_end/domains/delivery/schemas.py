"""Delivery domain schemas — DTOs, enums, requests, convergence state."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class PRState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class CIState(str, Enum):
    GREEN = "green"
    RED = "red"
    PENDING = "pending"
    EMPTY = "empty"


class VerdictResult(str, Enum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    NEEDS_HUMAN = "needs_human"


class ResolutionState(str, Enum):
    OPEN = "open"
    IN_FIX = "in-fix"
    FIXED = "fixed"
    VERIFIED = "verified"
    REOPENED = "reopened"
    DEFERRED = "deferred"
    WAIVED = "waived"
    REFUTED = "refuted"


class VerificationResult(str, Enum):
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    NEEDS_HUMAN = "needs-human"


class BlockingOverride(str, Enum):
    UNSET = ""
    BLOCK = "block"
    ALLOW = "allow"


class ACResult(BaseModel):
    criterion: str
    result: str  # pass | fail | unverifiable
    note: str = ""


# ── Convergence ──────────────────────────────────────────────────────────────


class ConvergenceCounts(BaseModel):
    blocking: int = 0
    deferred: int = 0
    waived: int = 0
    info: int = 0


class ConvergenceState(BaseModel):
    """Output of the convergence predicate (PLATFORM_V2_DESIGN.md §5)."""

    converged: bool
    head_sha: str = ""
    ci_state: CIState = CIState.EMPTY
    verdict_fresh: bool = False
    verdict_result: VerdictResult | None = None
    verdict_sha: str = ""
    clean_round: bool = False
    counts: ConvergenceCounts = Field(default_factory=ConvergenceCounts)
    reasons: list[str] = Field(default_factory=list)  # empty when converged


# ── DTOs ─────────────────────────────────────────────────────────────────────


class PullRequestDTO(BaseModel):
    uid: str
    repository_uid: str
    github_number: int
    title: str = ""
    author: str = ""
    url: str = ""
    state: PRState = PRState.OPEN
    draft: bool = False
    head_sha: str = ""
    head_ref: str = ""
    base_ref: str = ""
    base_is_default: bool = True
    ticket_uid: str = ""
    ci_state: CIState = CIState.EMPTY
    ci_checks: list[dict] = Field(default_factory=list)
    fix_rounds: int = 0
    # fix_rounds >= MergePolicy.max_fix_rounds — denormalized on the node at
    # every recompute + fix-run dispatch/reset ("human required" signal, §6).
    fix_rounds_exhausted: bool = False
    # Count of this PR's resolutions with a pending (not-yet-approved) waiver
    # request — lets the queue surface "needs you" without an N+1 per-PR fetch.
    waive_requested_count: int = 0
    converged: bool = False
    convergence: ConvergenceState | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_synced_at: datetime | None = None


class VerdictDTO(BaseModel):
    uid: str
    pull_request_uid: str
    repository_uid: str
    sha: str
    result: VerdictResult
    new_blocking_findings: int = 0
    finding_uids: list[str] = Field(default_factory=list)
    ac_results: list[ACResult] = Field(default_factory=list)
    source_run_uid: str = ""
    executor: str = "manual"
    # "" | pending | adjusted | superseded | failed (skeptic pass, §A)
    verification_status: str = ""
    verification_run_uid: str = ""
    created_at: datetime | None = None


class FindingVerificationDTO(BaseModel):
    uid: str
    pull_request_uid: str
    repository_uid: str
    verdict_uid: str
    finding_uid: str
    run_uid: str
    sha: str = ""
    result: VerificationResult
    reasoning: str = ""
    created_at: datetime | None = None


class FindingResolutionDTO(BaseModel):
    uid: str
    finding_uid: str
    pull_request_uid: str
    repository_uid: str
    introduced_at_sha: str = ""
    state: ResolutionState = ResolutionState.OPEN
    fixed_at_sha: str = ""
    verified_at_sha: str = ""
    verified_by_run_uid: str = ""
    waived_by: str = ""
    waive_reason: str = ""
    waive_requested_by: str = ""
    waive_requested_reason: str = ""
    blocking_override: BlockingOverride = BlockingOverride.UNSET
    blocking_override_reason: str = ""
    ticket_uid: str = ""
    blocking: bool = False  # computed against the repo MergePolicy
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # denormalized finding facets for triage views
    finding_title: str = ""
    finding_severity: str = "medium"
    finding_tags: list[str] = Field(default_factory=list)


class MergePolicyDTO(BaseModel):
    uid: str
    repository_uid: str
    blocking: dict = Field(default_factory=dict)
    require_clean_round: bool = True
    max_fix_rounds: int = 2
    # Write-path denylist — regex strings matched against changed paths (§6).
    path_denylist: list[str] = Field(default_factory=list)
    # Opt-in: ask GitHub to make `opensweep/converged` a required status check
    # on the default branch. Writes to the repository's GitHub *settings* and
    # affects every PR in the repo, so it is off unless a maintainer turns it on.
    enforce_converged_status: bool = False


# ── Requests ─────────────────────────────────────────────────────────────────


class AttachFixRequest(BaseModel):
    sha: str = Field(min_length=7)


class WaiveRequest(BaseModel):
    reason: str = Field(min_length=5)


class BlockingOverrideRequest(BaseModel):
    override: BlockingOverride
    reason: str = Field(min_length=5)


class SubmitVerdictRequest(BaseModel):
    sha: str = Field(min_length=7)
    result: VerdictResult
    new_blocking_findings: int = 0
    finding_uids: list[str] = Field(default_factory=list)
    ac_results: list[ACResult] = Field(default_factory=list)
    source_run_uid: str = ""
    executor: str = "manual"


class UpdateMergePolicyRequest(BaseModel):
    blocking: dict | None = None
    require_clean_round: bool | None = None
    max_fix_rounds: int | None = Field(default=None, ge=0, le=10)
    path_denylist: list[str] | None = None
    enforce_converged_status: bool | None = None
