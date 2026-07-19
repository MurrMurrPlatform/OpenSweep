"""Run node (PLATFORM_V3_DESIGN.md §2, §8).

A Run is a conversation with an agent in a workspace. Playbooks (chat / ask /
review / fix / implement / verify) determine the first prompt, mode, guards,
and per-turn completion hooks — internal machinery only; the user-facing
configuration concept is the Agent (domains/agents). Recurring/saved
configuration lives on ScheduledAgent; runs it spawns link back via
`scheduled_agent_uid`.
"""

from neomodel import (
    AsyncStructuredNode,
    DateTimeProperty,
    IntegerProperty,
    JSONProperty,
    StringProperty,
)


class Run(AsyncStructuredNode):
    """One conversation with an agent in a workspace (V3 §2).

    Replaces both V2's Run (one-shot execution) and Session
    (interactive chat). The first turn is the playbook's prompt; follow-up
    turns are accepted from awaiting_input AND the failure states — that is
    the recovery loop.
    """

    uid = StringProperty(unique_index=True, required=True)
    repository_uid = StringProperty(required=True, index=True)

    # chat | ask | review | fix | implement | verify
    playbook = StringProperty(default="ask", index=True)
    title = StringProperty(default="")

    # Where the run presents itself (orthogonal to playbook/trigger):
    # runs    — the Runs page (default; everything user-visible today)
    # comment — @opensweep comment replies, surfaced only inside their thread
    # chat    — opensweep chat-bubble conversations, surfaced only in the widget
    surface = StringProperty(default="runs", index=True)

    # Set only when a ScheduledAgent binding spawned the run.
    scheduled_agent_uid = StringProperty(default="", index=True)

    executor = StringProperty(required=True, index=True)
    execution_mode = StringProperty(default="analyze_only")
    run_policy_uid = StringProperty(index=True)
    provider_uid = StringProperty(default="", index=True)

    # Agent provenance — which Agent supplied this run's instructions layer,
    # and the org override revision active at dispatch (0 = platform body
    # as-is), so "why did the agent behave differently on this run" stays
    # answerable.
    agent_uid = StringProperty(default="", index=True)
    agent_rev = IntegerProperty(default=0)

    # queued | running | awaiting_input | ended | failed | cancelled |
    # limit_exceeded | paused_quota (provider limit — resumed by the beat task)
    status = StringProperty(default="queued", index=True)

    # Entity links (indexed for in-flight guards + entity pages).
    linked_pr_uid = StringProperty(default="", index=True)
    linked_ticket_uid = StringProperty(default="", index=True)
    linked_finding_uid = StringProperty(default="", index=True)
    # Set when a Thread dispatched (or adopted) this run — reverse lookup for
    # thread hooks (unified dev flow). "" for standalone runs.
    thread_uid = StringProperty(default="", index=True)
    # Playbook-specific blob (head_sha, node uids, paths, fix_round, …).
    target = JSONProperty(default={})

    # Workspace: the current live sandbox ("" when destroyed) plus everything
    # needed to recreate it (V3 §7): {purpose, source_branch, work_branch,
    # base_branch, cloned_at_sha, clone_depth}.
    sandbox_uid = StringProperty(default="", index=True)
    workspace_spec = JSONProperty(default={})

    # Conversation: CLI resume token (cleared when the workspace is recreated)
    # and the completed turn count. The transcript itself is the events.jsonl.
    cli_session_id = StringProperty(default="")
    turns = IntegerProperty(default=0)

    # Usage proxies — wall-time, tool turns, files touched, plus executor-
    # reported tokens/dollars when exposed.
    usage = JSONProperty(default={})

    # End-of-run outcome summary, written via the complete_run platform tool:
    # {text, did, skipped, succeeded, failed, next_steps}. The agent's own
    # structured summary wins over the platform's synthetic finalize text.
    summary = JSONProperty(default={})

    # Pointers to Findings, Knowledge updates, CoverageRecords, and artifacts
    # that this Run produced via the platform tool surface.
    output_refs = JSONProperty(default=[])

    # Raw executor output is ALWAYS retained, regardless of which return-
    # contract path succeeded.
    raw_artifact_uri = StringProperty(default="")

    # ok | degraded | failed
    parse_status = StringProperty(default="ok")

    # manual | event | schedule
    trigger = StringProperty(default="manual", index=True)
    triggered_by = StringProperty(default="")  # user_uid | event_uid | cron:<expr>

    error = StringProperty(default="")
    started_at = DateTimeProperty()
    completed_at = DateTimeProperty()
    last_activity_at = DateTimeProperty()
    ended_at = DateTimeProperty()
    duration_ms = IntegerProperty(default=0)

    created_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)


RUN_STATUSES = {
    "queued",
    "running",
    "awaiting_input",
    "ended",
    "failed",
    "cancelled",
    "limit_exceeded",
    "paused_quota",
}

PLAYBOOKS = {"chat", "ask", "review", "fix", "implement", "verify", "document", "thread"}

RUN_SURFACES = {"runs", "comment", "chat", "slack"}

EXECUTORS = {"internal_llm", "claude_code", "codex", "opencode", "manual"}

EXECUTION_MODES = {"analyze_only"}
