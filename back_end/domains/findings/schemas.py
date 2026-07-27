"""Finding DTOs."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel, Field

def normalize_tags(values: list[str] | None) -> list[str]:
    """Lowercase, kebab-ish, deduped free-text tags. Never a taxonomy —
    any label an agent or human finds useful for filtering is valid."""
    out: list[str] = []
    for raw in values or []:
        tag = str(raw or "").strip().lower().replace(" ", "-")[:40]
        if tag and tag not in out:
            out.append(tag)
    return out


class FindingKind(StrEnum):
    DEFECT = "defect"
    IMPROVEMENT = "improvement"
    GAP = "gap"
    PROPOSAL = "proposal"
    OBSERVATION = "observation"
    FEATURE_IDEA = "feature-idea"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingSize(StrEnum):
    """Fix-size estimate for a finding (how big the fix is) — distinct from
    the run effort tier (short/normal/deep/unlimited), which is a compute dial."""

    TRIVIAL = "trivial"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class FindingStatus(StrEnum):
    OPEN = "open"
    TICKETED = "ticketed"
    ACKNOWLEDGED = "acknowledged"
    WONT_FIX = "wont-fix"
    FIXED = "fixed"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"
    DISMISSED = "dismissed"


class SourcePath(StrEnum):
    TOOL_CALL = "tool-call"
    PARSED_BLOB = "parsed-blob"
    RAW_DERIVED = "raw-derived"


class ParseStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"


class FindingDTO(BaseModel):
    uid: str
    repository_uid: str

    tags: list[str] = Field(default_factory=list)
    kind: FindingKind
    severity: Severity = Severity.MEDIUM
    size: FindingSize = FindingSize.MEDIUM
    subtype: str = ""

    title: str
    confidence: float = 0.7
    description: str = ""
    root_cause: str = ""
    why_it_matters: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    suggested_fix: str = ""
    affected_paths: list[str] = Field(default_factory=list)

    dedupe_key: str

    source_run_uid: Optional[str] = None
    # Every run that filed or re-confirmed this finding (lazily backfilled
    # from source_run_uid) + when it was last re-found.
    source_run_uids: list[str] = Field(default_factory=list)
    last_confirmed_at: Optional[datetime] = None
    executor: str = "manual"
    source_path: SourcePath = SourcePath.TOOL_CALL
    parse_status: ParseStatus = ParseStatus.OK

    # Static-analysis provenance — which deterministic analyzer surfaced this
    # (empty for agent-discovered findings) and the rule/check id it carried.
    detected_by_tool: str = ""
    detected_by_rule: str = ""

    # Composite 0..1 credibility, DERIVED at the DTO boundary from signals the
    # platform can check independently of what the model asserted: how many
    # separate runs corroborated it, whether a static analyzer confirmed it,
    # the executor's own confidence, and any skeptic-pass outcome. Sortable
    # via sort_by="trust". See findings/services/trust.py.
    trust: float = 0.7

    # Provider that produced this finding, resolved from the source Run
    # when available. Empty for manual or pre-provider-tracking findings.
    provider_uid: Optional[str] = None
    provider_label: str = ""
    provider_kind: str = ""
    provider_model: str = ""

    status: FindingStatus = FindingStatus.OPEN

    # Ticket this finding was promoted into; set with status "ticketed" and
    # empty otherwise. Server-derived (see UpdateFindingRequest) — the UI reads
    # it only to render a forward link.
    ticket_uid: str = ""

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class FileFindingRequest(BaseModel):
    """Direct API entry. Executor-side tool calls go through the platform
    tool surface (Phase 4) instead of this route."""

    repository_uid: str
    tags: list[str] = Field(default_factory=list)
    kind: FindingKind = FindingKind.DEFECT
    severity: Severity = Severity.MEDIUM
    size: FindingSize = FindingSize.MEDIUM
    subtype: str = ""

    title: str
    confidence: float = 0.7
    description: str = ""
    root_cause: str = ""
    why_it_matters: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    suggested_fix: str = ""
    affected_paths: list[str] = Field(default_factory=list)

    detected_by_tool: str = ""
    detected_by_rule: str = ""

    source_run_uid: Optional[str] = None
    executor: str = "manual"


class UpdateFindingRequest(BaseModel):
    """Human correction of a finding's narrative/triage fields.

    Only editable prose + classification is exposed — machine provenance
    (confidence, evidence, detected_by_*, executor, dedupe_key, source) stays
    immutable so the audit trail of *how* the finding was surfaced is intact.
    Status has its own transition routes and is not editable here, and
    ticket_uid moves with it — both are set by promotion, not by hand. Every
    field is optional; only those sent are applied.
    """

    tags: Optional[list[str]] = None
    kind: Optional[FindingKind] = None
    severity: Optional[Severity] = None
    size: Optional[FindingSize] = None
    subtype: Optional[str] = None

    title: Optional[str] = None
    description: Optional[str] = None
    root_cause: Optional[str] = None
    why_it_matters: Optional[str] = None
    suggested_fix: Optional[str] = None
    affected_paths: Optional[list[str]] = None
