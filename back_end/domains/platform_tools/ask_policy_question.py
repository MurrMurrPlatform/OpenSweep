"""Platform tool: ask_policy_question.

A batch triage run reads thirty tickets at once. Most of its questions are
local and it answers them itself into the assumption ledger; a few are POLICY —
the answer changes how several tickets get written. Those are the only ones
worth a human's attention, and they should cost that attention once.

A separate tool from `ask_user`, not a `scope` flag on it. `ask_user` is
thread-bound at four independent layers — the route's `{thread_uid}` path
param, `resolve_thread` plus the phase gate, storage on `thread.events`, and
the ticket-comment mirror keyed on the thread's subject ticket. A flag would
have to no-op or fork all four: one name over two disjoint bodies. Separate
tools also let the registry description teach the classification, so the
distinction is expressed by WHICH tool the model picks rather than by a
free-text argument it can get wrong.

NON-BLOCKING by design. The question is recorded and surfaced; the run proceeds
under an assumption and the answer lands as a `Policy:` memory that the NEXT
batch is seeded with. Blocking would pin the run's sandbox until a human
replies, and "answer within the sandbox retention window or lose the context"
is a poor contract for a question someone may want to think about.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from infrastructure.audit import write_audit


async def ask_policy_question(
    *,
    question: str,
    applies_to_ticket_uids: list[str],
    options: list[str] | None = None,
    context: str = "",
    executor: str = "manual",
) -> dict[str, Any]:
    from domains.runs.models import Run

    text = (question or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="question must be non-empty")
    uids = [str(u).strip() for u in (applies_to_ticket_uids or []) if str(u).strip()]
    # This IS the policy test, and it is a 422 (unlike the question-cap
    # refusal, which is well-formed input at the wrong time): "policy" has to
    # be an assertion the agent backs with a list, not a label it awards
    # itself. A question that turns out to apply to one ticket is local, and
    # belongs in the assumption ledger.
    if len(set(uids)) < 2:
        raise HTTPException(
            status_code=422,
            detail=(
                "a policy question must name at least 2 tickets it changes in "
                "applies_to_ticket_uids — if it affects only one, answer it "
                "yourself and record it with record_assumption"
            ),
        )
    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    if len(opts) > 6:
        raise HTTPException(status_code=422, detail="at most 6 options")

    if not executor or executor == "manual":
        raise HTTPException(
            status_code=422, detail="ask_policy_question must be called from a run"
        )
    run = await Run.nodes.get_or_none(uid=executor)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {executor} not found")

    now = datetime.now(UTC)
    question_uid = uuid4().hex
    run.questions = [
        *(run.questions or []),
        {
            "ts": now.isoformat(),
            "type": "question",
            "uid": question_uid,
            "question": text,
            "options": opts,
            "context": (context or "")[:2000],
            "applies_to_ticket_uids": sorted(set(uids)),
            "status": "open",
            "answer": "",
            "answered_by": "",
        },
    ]
    await run.save()

    # Surfaced in the attention feed only. Deliberately NOT mirrored to ticket
    # comments the way ask_user is: a thread has exactly one ticket, but a
    # policy question spanning twelve would produce twelve comments for one
    # question.
    await write_audit(
        kind="run.policy_question_asked",
        subject_uid=run.uid,
        subject_type="Run",
        actor_uid=executor,
        repository_uid=run.repository_uid or "",
        payload={
            "title": text,
            "run_uid": run.uid,
            "question_uid": question_uid,
            "ticket_count": len(set(uids)),
        },
    )
    return {
        "run_uid": run.uid,
        "question_uid": question_uid,
        "status": "recorded",
        "instruction": (
            "Recorded for a human. Do NOT wait — proceed now under your best "
            "judgment and record an assumption for each affected ticket via "
            "`opensweep_platform_record_assumption`. The answer becomes a "
            "repository policy that the next batch is given up front."
        ),
    }
