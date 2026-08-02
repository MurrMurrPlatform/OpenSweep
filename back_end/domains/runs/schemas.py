"""Run DTOs."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Executor(StrEnum):
    CLAUDE_CODE = "claude_code"
    OPENCODE = "opencode"
    MANUAL = "manual"


class ExecutionMode(StrEnum):
    ANALYZE_ONLY = "analyze_only"
    # Phase 3 write path (§6): the agent edits + commits inside a write
    # sandbox; the platform (write_gate) validates and pushes. Never combined
    # with a discovery sandbox.
    IMPLEMENT = "implement"


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


class Effort(StrEnum):
    SHORT = "short"
    NORMAL = "normal"
    DEEP = "deep"
    UNLIMITED = "unlimited"


# Legacy stored/typed values → current tiers ("quick" predates the rename;
# "light"/"small"/"large" appear in older seeds and UI payloads).
_EFFORT_ALIASES = {
    "quick": Effort.SHORT,
    "light": Effort.SHORT,
    "small": Effort.SHORT,
    "large": Effort.DEEP,
}


def normalize_effort(value: str | None) -> Effort:
    """Tolerant parse for effort values from old rows, old clients, or seeds."""
    raw = (value or "").strip().lower()
    if raw in _EFFORT_ALIASES:
        return _EFFORT_ALIASES[raw]
    try:
        return Effort(raw)
    except ValueError:
        return Effort.NORMAL


class Autonomy(StrEnum):
    """How much judgment an agent may spend on the user's behalf.

    Peer of `Effort`, deliberately separate: effort is the COMPUTE dial (how
    long to work, how deep to look); autonomy is the ATTENTION dial (how much
    to ask). m0010 renamed `ScheduledAgent.compute_dial` → `autonomy` to keep
    exactly this kind of thing apart, so conflating the two here would undo it.

    The name is overloaded elsewhere and these are NOT the same vocabulary:
    `ScheduledAgent.autonomy` is a run-PERMISSION dial (disabled …
    auto-run-any), and `Repository.agent_autonomy` is a bool meaning "agents
    may transition ticket status".

    Only meaningful on runs that can reach a human — the `thread` playbook and
    batch triage. Every other playbook is already told there is no human
    (`prompt_kit.MCP_STARTUP_NOTE`).
    """

    INTERROGATE = "interrogate"  # today's behaviour: ask everything
    ASSUME = "assume"  # ask at most K; decide and record the rest
    STRICT = "strict"  # ask nothing; refuse rather than guess


def normalize_autonomy(value: str | None) -> Autonomy:
    """Tolerant parse for autonomy values from old rows or old clients.

    Falls back to INTERROGATE — the SAFEST tier, and today's behaviour — where
    `normalize_effort` falls back to the middle. The asymmetry is deliberate:
    an unreadable effort value costs compute, an unreadable autonomy value
    could cost a silently wrong product decision. `""` (every row predating the
    dial) therefore reads as "ask, like we always did".
    """
    try:
        return Autonomy((value or "").strip().lower())
    except ValueError:
        return Autonomy.INTERROGATE


#: Hard cap on clarifying questions per subject. None = uncapped.
#: ONE source, consumed by BOTH the prompt renderer (`autonomy_block`) and the
#: server-side enforcement in `ask_user` — the drift REASONING_TIER_DEFAULTS
#: exists to prevent.
AUTONOMY_QUESTION_CAPS: dict[Autonomy, int | None] = {
    Autonomy.INTERROGATE: None,
    Autonomy.ASSUME: 3,
    Autonomy.STRICT: 0,
}


# Default reasoning level per effort tier — used when the agent doesn't pin
# its own (Agent.reasoning == "").
REASONING_TIER_DEFAULTS = {
    Effort.SHORT: "low",
    Effort.NORMAL: "medium",
    Effort.DEEP: "high",
    Effort.UNLIMITED: "high",
}


def resolve_reasoning(agent_reasoning: str, effort: Effort) -> str:
    """The reasoning level stamped on a Run at dispatch: the agent's explicit
    override wins; anything else falls back to the effort tier's default."""
    override = (agent_reasoning or "").strip().lower()
    if override in {"low", "medium", "high"}:
        return override
    return REASONING_TIER_DEFAULTS[effort]


class RunDTO(BaseModel):
    uid: str
    repository_uid: str
    playbook: Playbook = Playbook.ASK
    title: str = ""
    scheduled_agent_uid: str = ""
    executor: Executor
    execution_mode: ExecutionMode = ExecutionMode.ANALYZE_ONLY
    run_policy_uid: str | None = None
    # Resolved effort tier + reasoning level snapshotted at dispatch;
    # "" = unknown/legacy.
    effort: str = ""
    reasoning: str = ""
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
    # Agent that supplied this run's instructions layer at dispatch, plus
    # the org override revision active then (""/0 = resolved from code
    # fallbacks) — run provenance for "why did the agent behave differently
    # on this run".
    agent_uid: str = ""
    agent_rev: int = 0
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


class RunHandoffDTO(BaseModel):
    """Terminal takeover payload (docs/features/sandbox_improvements.md §3A/3B).

    mode: resume — paste-command resumes the actual claude session;
          seeded — paste-command starts a fresh claude seeded by the
          OPENSWEEP_HANDOFF.md brief written into the workspace;
          unavailable — no live workspace; `reason` says how to recover.
    """

    mode: str
    command: str = ""
    sandbox_host_path: str = ""
    cli_session_id: str = ""
    reason: str = ""


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
