"""Investigation + Run DTOs."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Executor(StrEnum):
    INTERNAL_LLM = "internal_llm"
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    OPENCODE = "opencode"
    MANUAL = "manual"


class ExecutionMode(StrEnum):
    ANALYZE_ONLY = "analyze_only"
    # Phase 3 write path (§6): the agent edits + commits inside a write
    # sandbox; the platform (write_gate) validates and pushes. Never combined
    # with a discovery sandbox.
    IMPLEMENT = "implement"


class ComputeDial(StrEnum):
    DISABLED = "disabled"
    SUGGEST = "suggest"
    ASK_BEFORE_RUN = "ask-before-run"
    AUTO_RUN_CHEAP = "auto-run-cheap"
    AUTO_RUN_ANY = "auto-run-any"


class InvestigationProvenance(StrEnum):
    HUMAN_ASKED = "human-asked"
    LLM_PROPOSED = "llm-proposed"
    TEMPLATE = "template"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    # A turn finished — the run does NOT die (V3 §2): the workspace stays
    # alive, the composer is enabled, follow-up turns are accepted.
    AWAITING_INPUT = "awaiting_input"
    # User closed the run, or the retention sweep expired the workspace.
    # Terminal only for the workspace — a follow-up reopens the conversation.
    ENDED = "ended"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LIMIT_EXCEEDED = "limit_exceeded"
    # Quota is a state, not a failure (PLATFORM_V2_DESIGN.md §8): the provider
    # hit its usage/rate limit; the run keeps its sandbox and is re-dispatched
    # by the resume beat task (fallback provider or after the reset window).
    PAUSED_QUOTA = "paused_quota"


class Playbook(StrEnum):
    CHAT = "chat"
    ASK = "ask"
    REVIEW = "review"
    FIX = "fix"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    DOCUMENT = "document"
    REFINE = "refine"


# States from which a follow-up message is accepted (V3 §2). Replying to a
# failed run is the recovery loop; replying to an ended run reopens it
# (workspace recreated on demand).
FOLLOW_UP_STATUSES = {
    RunStatus.AWAITING_INPUT,
    RunStatus.ENDED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.LIMIT_EXCEEDED,
}


class RunTrigger(StrEnum):
    MANUAL = "manual"
    EVENT = "event"
    SCHEDULE = "schedule"


class InvestigationEffort(StrEnum):
    SHORT = "short"
    NORMAL = "normal"
    DEEP = "deep"
    UNLIMITED = "unlimited"


# Legacy stored/typed values → current tiers ("quick" predates the rename).
_EFFORT_ALIASES = {"quick": InvestigationEffort.SHORT}


def normalize_effort(value: str | None) -> InvestigationEffort:
    """Tolerant parse for effort values from old rows, old clients, or seeds."""
    raw = (value or "").strip().lower()
    if raw in _EFFORT_ALIASES:
        return _EFFORT_ALIASES[raw]
    try:
        return InvestigationEffort(raw)
    except ValueError:
        return InvestigationEffort.NORMAL


SCHEDULE_MANUAL = ""
SCHEDULE_ON_EVENT = "on-event"
SCHEDULE_CRON_PREFIX = "cron:"


def parse_schedule(value: str | None) -> tuple[str, str]:
    """Return (kind, payload) for a schedule string.

    kind ∈ {"manual", "on-event", "cron"}; payload is the cron expression
    (empty otherwise). Raises ValueError on a "cron:" prefix with no expression.
    """
    raw = (value or "").strip()
    if not raw:
        return ("manual", "")
    if raw == SCHEDULE_ON_EVENT:
        return ("on-event", "")
    if raw.startswith(SCHEDULE_CRON_PREFIX):
        expr = raw[len(SCHEDULE_CRON_PREFIX) :].strip()
        if not expr:
            raise ValueError("cron schedule requires a crontab expression after 'cron:'")
        return ("cron", expr)
    raise ValueError(f"unknown schedule format: {raw!r}")


class InvestigationDTO(BaseModel):
    uid: str
    repository_uid: str
    title: str = ""
    description: str = ""
    intent: str
    job_type: str = "audit"
    target: dict[str, Any] = Field(default_factory=dict)
    effort: InvestigationEffort = InvestigationEffort.NORMAL
    schedule: str = ""
    default_executor: Executor = Executor.INTERNAL_LLM
    default_mode: ExecutionMode = ExecutionMode.ANALYZE_ONLY
    run_policy_uid: str | None = None
    provenance: InvestigationProvenance = InvestigationProvenance.HUMAN_ASKED
    compute_dial: ComputeDial = ComputeDial.ASK_BEFORE_RUN
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("effort", mode="before")
    @classmethod
    def _normalize_effort(cls, v):
        if v is None:
            return v
        return normalize_effort(v if isinstance(v, str) else (v.value if v else ""))


class CreateInvestigationRequest(BaseModel):
    repository_uid: str
    title: str = ""
    description: str = ""
    intent: str = ""
    job_type: str = "audit"
    target: dict[str, Any] = Field(default_factory=dict)
    effort: InvestigationEffort = InvestigationEffort.NORMAL
    schedule: str = ""
    default_executor: Executor = Executor.INTERNAL_LLM
    default_mode: ExecutionMode = ExecutionMode.ANALYZE_ONLY
    run_policy_uid: str | None = None
    compute_dial: ComputeDial = ComputeDial.ASK_BEFORE_RUN

    @field_validator("effort", mode="before")
    @classmethod
    def _normalize_effort(cls, v):
        if v is None:
            return v
        return normalize_effort(v if isinstance(v, str) else (v.value if v else ""))


class UpdateInvestigationRequest(BaseModel):
    """PATCH /investigations/{uid} — None = leave unchanged. Covers the
    scheduling surface (cron/dial/effort/target.limit) plus title/intent."""

    title: str | None = None
    description: str | None = None
    intent: str | None = None
    target: dict[str, Any] | None = None
    effort: InvestigationEffort | None = None
    schedule: str | None = None
    compute_dial: ComputeDial | None = None

    @field_validator("effort", mode="before")
    @classmethod
    def _normalize_effort(cls, v):
        if v is None:
            return v
        return normalize_effort(v if isinstance(v, str) else (v.value if v else ""))


class RunDTO(BaseModel):
    uid: str
    repository_uid: str
    playbook: Playbook = Playbook.ASK
    title: str = ""
    investigation_uid: str = ""
    executor: Executor
    execution_mode: ExecutionMode = ExecutionMode.ANALYZE_ONLY
    run_policy_uid: str | None = None
    status: RunStatus = RunStatus.QUEUED
    linked_pr_uid: str = ""
    linked_ticket_uid: str = ""
    linked_finding_uid: str = ""
    target: dict[str, Any] = Field(default_factory=dict)
    sandbox_uid: str = ""
    workspace_spec: dict[str, Any] = Field(default_factory=dict)
    cli_session_id: str = ""
    turns: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    # LLM provider that executed this run, snapshotted from `usage.provider_*`
    # at run-start time. Empty for runs predating the snapshot.
    provider_uid: str | None = None
    provider_label: str = ""
    provider_kind: str = ""
    provider_model: str = ""
    # Org agent overlay active for this run's org+playbook at dispatch
    # (""/0 = none applied) — run provenance for "why did the agent behave
    # differently on this run".
    overlay_uid: str = ""
    overlay_rev: int = 0
    # End-of-run outcome summary from complete_run:
    # {text, did, skipped, succeeded, failed, next_steps}
    summary: dict[str, Any] = Field(default_factory=dict)
    output_refs: list[str] = Field(default_factory=list)
    raw_artifact_uri: str = ""
    parse_status: str = "ok"
    trigger: RunTrigger = RunTrigger.MANUAL
    triggered_by: str = ""
    # runs | comment | chat — which UI surface owns this run (old nodes
    # predate the field and read as "runs").
    surface: str = "runs"
    error: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_activity_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CreateRunRequest(BaseModel):
    """POST /api/v1/runs — one-off run creation (chat and ask playbooks; the
    other playbooks have domain trigger endpoints that carry their guards)."""

    # May be blank ONLY for surface=chat with a context subject — the
    # subject's repository becomes the run's repository.
    repository_uid: str = ""
    playbook: Playbook = Playbook.CHAT
    prompt: str = ""
    title: str = ""
    target: dict[str, Any] = Field(default_factory=dict)
    linked_pr_uid: str = ""
    linked_ticket_uid: str = ""
    linked_finding_uid: str = ""
    executor: Executor | None = None
    # "chat" marks a opensweep chat-bubble conversation (hidden from the Runs
    # page); only valid with playbook=chat. Default keeps runs visible.
    surface: str = "runs"
    # What the user was looking at when the chat started — becomes a
    # snapshot in the first-turn preamble. {subject_type, subject_uid}.
    context: dict[str, str] = Field(default_factory=dict)
    provider_uid: str = ""


class SendRunMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


class RunMessageResult(BaseModel):
    """REST fallback result for one follow-up turn."""

    content: str = ""
    status: RunStatus = RunStatus.AWAITING_INPUT
    interrupted: bool = False
    error: str = ""


class ClientWsMessage(BaseModel):
    """Client → server frame on WS /runs/{uid}/ws."""

    type: str = "message"  # message | interrupt
    text: str = ""
