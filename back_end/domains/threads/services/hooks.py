"""Thread lifecycle hooks — called from delivery finalizers and webhooks.

Hook failures never corrupt the host flow (logged, not raised), mirroring
domains/investigations/services/playbooks.py.
"""

from __future__ import annotations

from logging_config import logger


async def _advance_to_in_review(run) -> None:
    from domains.delivery.models import PullRequest
    from domains.threads.services.thread_service import ThreadService

    svc = ThreadService()
    thread = await svc.get_node(run.thread_uid)
    if thread.phase != "implementing":
        return  # already advanced (idempotent) or user abandoned
    pr = await PullRequest.nodes.get_or_none(uid=run.linked_pr_uid)
    thread.pr_uid = run.linked_pr_uid or ""
    thread.branch = (getattr(pr, "head_ref", "") or "") or thread.branch
    await thread.save()
    await svc.transition(thread.uid, "in_review", actor_uid="system")
    await svc.record_event(thread, "pr_opened", pr_uid=thread.pr_uid)


async def note_pr_opened_for_run(run) -> None:
    """Implement finalizer hook: PR exists → thread moves to in_review."""
    if not (getattr(run, "thread_uid", "") or "") or not (run.linked_pr_uid or ""):
        return
    try:
        await _advance_to_in_review(run)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"thread hook pr_opened failed for run {run.uid}: {type(exc).__name__}: {exc}",
            extra={"tag": "threads"},
        )


async def note_verdict_for_pr(pr_uid: str, *, result: str, verdict_uid: str, sha: str) -> None:
    """Verdict submitted → timeline event on every active thread for this PR."""
    if not (pr_uid or "").strip():
        return
    try:
        from domains.threads.models import Thread
        from domains.threads.services.thread_service import ThreadService

        svc = ThreadService()
        for thread in await Thread.nodes.filter(pr_uid=pr_uid).all():
            if thread.phase == "in_review":
                await svc.record_event(
                    thread,
                    "review_verdict",
                    result=result,
                    verdict_uid=verdict_uid,
                    sha=sha,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"thread hook verdict failed for PR {pr_uid}: {type(exc).__name__}: {exc}",
            extra={"tag": "threads"},
        )


async def note_fix_run_for_pr(pr_uid: str, run) -> None:
    """Fix run dispatched → the thread's conversation continues with the fixer.

    Attaches the fix run to every active thread on this PR (sets
    active_run_uid so the thread chat targets the fix conversation) and
    records a fix_started event.
    """
    if not (pr_uid or "").strip():
        return
    try:
        from domains.threads.models import Thread
        from domains.threads.services.thread_service import ThreadService

        svc = ThreadService()
        for thread in await Thread.nodes.filter(pr_uid=pr_uid).all():
            if thread.phase == "in_review":
                await svc.attach_run(thread, run.uid)
                await svc.record_event(thread, "fix_started", run_uid=run.uid)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"thread hook fix_started failed for PR {pr_uid}: {type(exc).__name__}: {exc}",
            extra={"tag": "threads"},
        )


async def note_pr_merged(pr_uid: str) -> None:
    """Merge webhook hook: every in_review thread on this PR → done."""
    if not (pr_uid or "").strip():
        return
    try:
        from domains.threads.models import Thread
        from domains.threads.services.thread_service import ThreadService

        svc = ThreadService()
        for thread in await Thread.nodes.filter(pr_uid=pr_uid).all():
            if thread.phase == "in_review":
                await svc.transition(thread.uid, "done", actor_uid="system")
                await svc.record_event(thread, "merged", pr_uid=pr_uid)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"thread hook pr_merged failed for PR {pr_uid}: {type(exc).__name__}: {exc}",
            extra={"tag": "threads"},
        )
