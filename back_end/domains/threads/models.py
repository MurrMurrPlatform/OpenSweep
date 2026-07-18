"""Thread — one piece of work moving through the pipeline, with one
conversation attached (docs/superpowers/specs/2026-07-18-unified-dev-flow-design.md).

A Thread binds a ticket (or group parent) to a continuous refine → plan →
implement conversation. It orchestrates and references Runs; it never
replaces them. Review runs stay independent — their output attaches to the
timeline. Phase only ever moves through the service so every move is
legality-checked and audited.
"""

from neomodel import (
    AsyncStructuredNode,
    DateTimeProperty,
    IntegerProperty,
    JSONProperty,
    StringProperty,
)


class Thread(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    repository_uid = StringProperty(required=True, index=True)
    subject_ticket_uid = StringProperty(required=True, index=True)

    # refining | implementing | in_review | done | abandoned
    phase = StringProperty(default="refining", index=True)

    # Soft plan gate: none | drafted | approved. Approval is NOT required to
    # implement; an approved plan is injected into the implement run context.
    plan_state = StringProperty(default="none")
    plan_text = StringProperty(default="")
    plan_approved_by = StringProperty(default="")
    plan_approved_at = DateTimeProperty()

    # Delivery links, filled as the flow progresses.
    branch = StringProperty(default="")
    pr_uid = StringProperty(default="", index=True)

    # Conversation: ordered run uids + the run currently accepting messages.
    run_uids = JSONProperty(default=[])
    active_run_uid = StringProperty(default="")

    # Timeline events: [{ts, type, ...payload}] — phase_changed, plan_drafted,
    # plan_approved, run_attached, pr_opened, merged, abandoned.
    events = JSONProperty(default=[])

    # Native executor todo lists (Claude Code TodoWrite, Codex update_plan,
    # OpenCode todowrite), mirrored per phase from the transcript after every
    # turn: {phase: [{content, status, activeForm}]}. The union across phases
    # is the thread's "big" todo list.
    todos = JSONProperty(default={})
    # Last transcript seq already scanned for todo updates (incremental).
    todos_seq = IntegerProperty(default=0)

    created_by = StringProperty(default="")
    created_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)


THREAD_PHASES = {"refining", "implementing", "in_review", "done", "abandoned"}

PLAN_STATES = {"none", "drafted", "approved"}

LEGAL_PHASE_TRANSITIONS: dict[str, frozenset[str]] = {
    "refining": frozenset({"implementing", "abandoned"}),
    "implementing": frozenset({"in_review", "abandoned"}),
    "in_review": frozenset({"done", "abandoned"}),
    "done": frozenset(),
    "abandoned": frozenset(),
}


def is_legal_phase_transition(frm: str, to: str) -> bool:
    return to in LEGAL_PHASE_TRANSITIONS.get(frm, frozenset())
