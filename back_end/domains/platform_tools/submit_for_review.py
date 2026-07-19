"""Platform tool: submit_for_review.

The thread session agent signals its work is complete and ready for review.
The tool only sets the thread's `ready_for_review` flag (idempotent) and
records a timeline event — the platform reacts at the turn boundary
(threads/services/hooks.maybe_ready_and_review_for_thread): un-draft the PR
and, when the repo's workflow has review on auto, dispatch the review run.
The agent never dispatches runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from infrastructure.audit import write_audit

_READY_PHASES = {"implementing", "in_review"}


async def submit_for_review(
    *,
    thread_uid: str,
    executor: str = "manual",
) -> dict[str, Any]:
    if not (thread_uid or "").strip():
        raise HTTPException(status_code=422, detail="thread_uid is required")
    from domains.threads.services.thread_service import (
        THREAD_NOT_FOUND_DETAIL,
        resolve_thread,
    )

    thread = await resolve_thread(thread_uid, run_uid=executor)
    if thread is None:
        raise HTTPException(status_code=404, detail=THREAD_NOT_FOUND_DETAIL)
    thread_uid = thread.uid  # the candidate may have been the ticket uid
    if thread.phase not in _READY_PHASES:
        raise HTTPException(
            status_code=409,
            detail=(
                "ready-for-review only applies while implementing or in review — "
                f"thread is '{thread.phase}'"
            ),
        )
    if not thread.ready_for_review:
        now = datetime.now(UTC)
        thread.ready_for_review = True
        thread.events = [
            *(thread.events or []),
            {"ts": now.isoformat(), "type": "ready_for_review", "by": executor},
        ]
        thread.updated_at = now
        await thread.save()
        await write_audit(
            kind="thread.ready_for_review",
            subject_uid=thread_uid,
            subject_type="Thread",
            actor_uid=executor,
            payload={"phase": thread.phase},
        )
    return {
        "thread_uid": thread_uid,
        "ready_for_review": True,
        "phase": thread.phase,
        "note": (
            "recorded — after this turn the platform pushes your work, marks "
            "the PR ready and (if configured) dispatches the review; its "
            "findings will arrive in this conversation"
        ),
    }
