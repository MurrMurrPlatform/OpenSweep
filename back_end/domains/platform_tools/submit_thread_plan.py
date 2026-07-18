"""Platform tool: submit_thread_plan.

The thread session agent persists its implementation plan here. Sets the
thread's plan to `drafted` (idempotent re-submits allowed while the thread is
still refining) and records a timeline event. Approval stays human-only —
this tool can never set `approved`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from infrastructure.audit import write_audit


def _validate(*, thread_uid: str, plan_markdown: str) -> None:
    if not (thread_uid or "").strip():
        raise HTTPException(status_code=422, detail="thread_uid is required")
    if not (plan_markdown or "").strip():
        raise HTTPException(status_code=422, detail="plan_markdown must not be empty")


async def submit_thread_plan(
    *,
    thread_uid: str,
    plan_markdown: str,
    executor: str = "manual",
) -> dict[str, Any]:
    from domains.threads.models import Thread

    _validate(thread_uid=thread_uid, plan_markdown=plan_markdown)
    from domains.threads.services.thread_service import (
        THREAD_NOT_FOUND_DETAIL,
        resolve_thread,
    )

    thread = await resolve_thread(thread_uid, run_uid=executor)
    if thread is None:
        raise HTTPException(status_code=404, detail=THREAD_NOT_FOUND_DETAIL)
    thread_uid = thread.uid  # the candidate may have been the ticket uid
    if thread.phase != "refining":
        raise HTTPException(
            status_code=409,
            detail=f"plan can only be drafted while refining — thread is '{thread.phase}'",
        )
    now = datetime.now(UTC)
    thread.plan_text = plan_markdown
    thread.plan_state = "drafted"
    thread.events = [
        *(thread.events or []),
        {"ts": now.isoformat(), "type": "plan_drafted", "by": executor},
    ]
    thread.updated_at = now
    await thread.save()

    # Canonical public copy lives on the ticket (user request: plan as
    # ticket metadata, not buried in the conversation).
    from domains.threads.services.thread_service import mirror_plan_to_ticket

    await mirror_plan_to_ticket(thread)

    await write_audit(
        kind="thread.plan_drafted",
        subject_uid=thread_uid,
        subject_type="Thread",
        actor_uid=executor,
        payload={"chars": len(plan_markdown)},
    )
    return {"thread_uid": thread_uid, "plan_state": "drafted"}
