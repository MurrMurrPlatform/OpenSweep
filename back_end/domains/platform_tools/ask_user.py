"""Platform tool: ask_user.

The thread session agent asks the user a STRUCTURED question instead of
burying it in prose: the question (plus optional multiple-choice options)
lands on the thread as a `question` event, the thread UI renders it as an
answer card, and the user's answer resumes the conversation as a normal
follow-up turn. Distinct from `ask_question` (deep-scan Analysis surface).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from infrastructure.audit import write_audit


def asked_question_count(events: list[dict]) -> int:
    """Questions this thread has already spent, over its LIFETIME.

    Lifetime, not per-turn: a per-turn count would let the agent ask K
    questions every turn forever, which is the behaviour the cap exists to
    stop. Dismissed questions still count — the user paid attention to them.
    """
    return sum(1 for e in events or [] if e.get("type") == "question")


def question_cap_refusal(events: list[dict], *, autonomy: str) -> dict | None:
    """The refusal payload when this thread has spent its question budget.

    Returns None when the agent may still ask.

    Deliberately a 200 with a payload, NOT a 4xx. The existing `_validate`
    422s are for MALFORMED input; a cap breach is well-formed input arriving at
    the wrong time, and some harnesses read a 4xx tool result as a transport
    error and retry-loop on it. The payload carries the redirect, which is the
    whole point: it converts a blocked question into a ledger entry instead of
    a stalled turn.

    Enforced server-side rather than left to the prompt because the failure is
    asymmetric. An agent that ignores a prompt-level FINDING cap files 8
    findings instead of 5 — cheap. An agent that ignores a question cap ends
    its turn 8 times and leaves a human staring at 8 answer cards on a ticket
    that is going nowhere, which is exactly the state `assume` exists to
    prevent. Precedent: `_validate` already enforces `len(options) > 6` here
    rather than trusting the prompt.
    """
    from domains.runs.services.autonomy import question_cap

    cap = question_cap(autonomy)
    if cap is None:
        return None
    asked = asked_question_count(events)
    if asked < cap:
        return None
    return {
        "status": "capped",
        "question_uid": "",
        "asked": asked,
        "cap": cap,
        "instruction": (
            f"Question budget for this thread is spent ({asked}/{cap}, "
            f"autonomy={autonomy or 'assume'}). Do NOT ask this — answer it "
            "yourself with the most conventional choice for this codebase, "
            "cite the file:line precedent, and record it via "
            "`opensweep_platform_record_assumption`. The reviewer sees every "
            "assumption you record."
        ),
    }


def _validate(*, thread_uid: str, question: str, options: list[str]) -> None:
    if not (thread_uid or "").strip():
        raise HTTPException(status_code=422, detail="thread_uid is required")
    if not (question or "").strip():
        raise HTTPException(status_code=422, detail="question must be non-empty")
    if len(options) > 6:
        raise HTTPException(status_code=422, detail="at most 6 options")


async def ask_user(
    *,
    thread_uid: str,
    question: str,
    options: list[str] | None = None,
    context: str = "",
    executor: str = "manual",
) -> dict[str, Any]:
    from domains.threads.models import Thread

    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    _validate(thread_uid=thread_uid, question=question, options=opts)
    from domains.threads.services.thread_service import (
        THREAD_NOT_FOUND_DETAIL,
        resolve_thread,
    )

    thread = await resolve_thread(thread_uid, run_uid=executor)
    if thread is None:
        raise HTTPException(status_code=404, detail=THREAD_NOT_FOUND_DETAIL)
    thread_uid = thread.uid  # the candidate may have been the ticket uid
    if thread.phase in {"done", "abandoned"}:
        raise HTTPException(status_code=409, detail=f"thread is {thread.phase}")

    capped = question_cap_refusal(
        thread.events or [], autonomy=getattr(thread, "autonomy", "") or ""
    )
    if capped is not None:
        return capped
    now = datetime.now(UTC)
    question_uid = uuid4().hex
    thread.events = [
        *(thread.events or []),
        {
            "ts": now.isoformat(),
            "type": "question",
            "uid": question_uid,
            "question": question.strip(),
            "options": opts,
            "context": (context or "")[:2000],
            "status": "open",
            "answer": "",
            "answered_by": "",
        },
    ]
    thread.updated_at = now
    await thread.save()

    # The run is now literally waiting on a human answer: surface it in the
    # notification feed (attention group) and flag the calling run so the UI
    # can show "needs your input" instead of a generic finished badge. The
    # repository_uid is passed explicitly — deriving it would need another
    # DB round-trip write_audit can skip.
    await write_audit(
        kind="thread.question_asked",
        subject_uid=thread.uid,
        subject_type="Thread",
        actor_uid=executor,
        repository_uid=thread.repository_uid or "",
        payload={
            "title": question.strip(),
            "thread_uid": thread.uid,
            "question_uid": question_uid,
            "ticket_uid": thread.subject_ticket_uid or "",
            "run_uid": executor if executor != "manual" else "",
        },
    )
    if executor and executor != "manual":
        from domains.runs.models import Run

        run = await Run.nodes.get_or_none(uid=executor)
        if run is not None:
            run.usage = {**(run.usage or {}), "needs_input": True}
            await run.save()

    # Mirror to the ticket's discussion so followers see the question without
    # opening the thread (the agent itself cannot post comments — the
    # platform does, deterministically). Carries machine meta so the UI can
    # render quick-reply chips and a reply routes back as the answer.
    # Best-effort.
    try:
        from domains.comments import service as comment_service
        from domains.comments.schemas import CommentAuthorKind, CommentSubjectType

        opts_block = "\n".join(f"- {o}" for o in opts)
        body = (
            f"❓ **Agent question** (from the ticket's thread)\n\n{question.strip()}"
            + (f"\n\n{opts_block}" if opts_block else "")
            + "\n\n_Reply to this comment — or answer in the thread — and the "
            "conversation continues._"
        )
        mirror = await comment_service.create_comment(
            subject_type=CommentSubjectType.TICKET,
            subject_uid=thread.subject_ticket_uid,
            body=body,
            author_uid=executor,
            author_kind=CommentAuthorKind.OPENSWEEP,
            source_run_uid=executor if executor != "manual" else "",
            meta={
                "kind": "thread_question",
                "thread_uid": thread_uid,
                "question_uid": question_uid,
                "options": opts,
                "status": "open",
            },
        )
        # Back-link so answering (either side) can sync the other.
        events = list(thread.events or [])
        for event in events:
            if event.get("type") == "question" and event.get("uid") == question_uid:
                event["comment_uid"] = mirror.uid
                break
        thread.events = events
        await thread.save()
    except Exception:  # noqa: BLE001 — mirroring must never fail the question
        pass

    return {"thread_uid": thread_uid, "question_uid": question_uid, "status": "open"}
