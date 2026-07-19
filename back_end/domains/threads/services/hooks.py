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


async def maybe_ready_and_review_for_thread(thread_uid: str) -> None:
    """Turn-boundary reaction to the ready-for-review signal (agent tool
    `submit_for_review` or the human request-review endpoint): sync the PR,
    flip it out of draft, and dispatch the review when the repo's workflow
    has review on auto. Deterministic and idempotent — fires after every
    thread turn; the guards make repeat calls no-ops. Never raises (hook
    contract), and never dispatches when `workflow.review.auto` is off:
    the signal then only un-drafts and the review stays a manual trigger."""
    if not (thread_uid or "").strip():
        return
    try:
        from domains.delivery.models import PullRequest
        from domains.delivery.services.pull_request_service import (
            PullRequestService,
            latest_verdict_for,
        )
        from domains.delivery.services.review_run_service import trigger_review_run
        from domains.investigations.schemas import RunTrigger
        from domains.repositories.models import Repository
        from domains.repositories.services.workflow import stage_auto
        from domains.threads.models import Thread
        from infrastructure.git_providers import get_provider_client

        thread = await Thread.nodes.get_or_none(uid=thread_uid)
        if (
            thread is None
            or not thread.ready_for_review
            or thread.phase != "in_review"
            or not (thread.pr_uid or "")
        ):
            return
        pr = await PullRequest.nodes.get_or_none(uid=thread.pr_uid)
        if pr is None:
            return

        service = PullRequestService()
        # Refresh from GitHub first — the platform just pushed, and without
        # webhooks head_sha/draft would go stale. Best-effort: an unreachable
        # provider must not block the dispatch decision on known state.
        try:
            await service.sync_from_github(pr.repository_uid, int(pr.github_number))
            pr = await PullRequest.nodes.get_or_none(uid=thread.pr_uid) or pr
        except Exception as exc:  # noqa: BLE001
            logger.info(
                f"PR sync skipped for thread {thread_uid}: {type(exc).__name__}: {exc}",
                extra={"tag": "threads"},
            )
        if pr.state != "open":
            return

        if pr.draft:
            try:
                repo = await Repository.nodes.get_or_none(uid=pr.repository_uid)
                client = get_provider_client(repo) if repo is not None else None
                if client is not None and client.is_active:
                    await client.mark_pull_request_ready(
                        repo.github_owner, repo.github_repo, int(pr.github_number)
                    )
                    pr.draft = False
                    await pr.save()
                    await service.recompute_and_publish(pr)
            except Exception as exc:  # noqa: BLE001
                # The auto-fix draft exemption for thread-owned PRs keeps the
                # loop alive even when un-draft fails (token scope, GHES) —
                # log and continue to the review dispatch.
                logger.warning(
                    f"un-draft failed for PR #{pr.github_number}: {type(exc).__name__}: {exc}",
                    extra={"tag": "threads"},
                )

        if not await stage_auto(pr.repository_uid, "review"):
            return
        if not (pr.head_sha or ""):
            return
        verdict = await latest_verdict_for(pr.uid, head_sha=pr.head_sha)
        if verdict is not None and (verdict.sha or "") == pr.head_sha:
            return  # this head is already reviewed — nothing to re-review
        try:
            run = await trigger_review_run(
                pr, triggered_by="thread-ready", trigger=RunTrigger.EVENT
            )
            logger.info(
                f"review run {run.uid} dispatched for PR #{pr.github_number} "
                f"(thread {thread_uid} ready)",
                extra={"tag": "threads"},
            )
        except Exception as exc:  # noqa: BLE001
            # 409s are the in-flight guard doing its job (review running,
            # or the webhook beat us to it) — normal control flow.
            detail = getattr(exc, "detail", None) or str(exc)
            logger.info(
                f"review dispatch skipped for PR #{pr.github_number}: {detail}",
                extra={"tag": "threads"},
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"thread hook ready_and_review failed for {thread_uid}: {type(exc).__name__}: {exc}",
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
