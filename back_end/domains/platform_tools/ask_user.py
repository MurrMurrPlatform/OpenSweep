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
    thread = await Thread.nodes.get_or_none(uid=thread_uid)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    if thread.phase in {"done", "abandoned"}:
        raise HTTPException(status_code=409, detail=f"thread is {thread.phase}")
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

    # Mirror to the ticket's discussion so followers see the question without
    # opening the thread (the agent itself cannot post comments — the
    # platform does, deterministically). Best-effort.
    try:
        from domains.comments import service as comment_service
        from domains.comments.schemas import CommentAuthorKind, CommentSubjectType

        opts_block = "\n".join(f"- {o}" for o in opts)
        body = (
            f"❓ **Agent question** (from the ticket's thread)\n\n{question.strip()}"
            + (f"\n\n{opts_block}" if opts_block else "")
            + "\n\n_Answer it in the thread — the answer will be posted back here._"
        )
        await comment_service.create_comment(
            subject_type=CommentSubjectType.TICKET,
            subject_uid=thread.subject_ticket_uid,
            body=body,
            author_uid=executor,
            author_kind=CommentAuthorKind.OPENSWEEP,
            source_run_uid=executor if executor != "manual" else "",
        )
    except Exception:  # noqa: BLE001 — mirroring must never fail the question
        pass

    return {"thread_uid": thread_uid, "question_uid": question_uid, "status": "open"}
