"""Playbook registry (PLATFORM_V3_DESIGN.md §3).

A playbook is code, not a DB entity: the domain trigger endpoints supply the
first-turn prompt and dispatch guards; this module supplies the per-turn
completion hooks, resolved from the run itself so they can fire after EVERY
turn (initial adapter dispatch and follow-up turns alike) — closures can't be
persisted across turns, run fields can.

Hooks by playbook:
    chat                — none (conversation only)
    ask / verify / document / refine — Checked stamp (KNOWLEDGE_V3_CHECKED.md §3)
    refine              — read-only triage/enrichment: the agent judges whether
                          a Finding/Ticket is real, improves its title /
                          description / acceptance criteria and attaches
                          relevant files + a plan, writing straight back via the
                          platform tools (update_finding / update_ticket /
                          attach_artifact) — so there is no finalizer, only the
                          Checked stamp.
    review              — Checked stamp; verdict recompute happens in-band;
                          auto-fix chain when workflow.fix.auto is on and the
                          fresh verdict is request_changes (bounded by
                          MergePolicy.max_fix_rounds via trigger_fix_run's
                          own guards)
    fix                 — write gate: validate sandbox → push same branch
    implement           — write gate: validate → push → open draft PR

Hook failures never corrupt the recorded run outcome (logged, not raised).
"""

from __future__ import annotations

from domains.runs.models import Run
from logging_config import logger

PLAYBOOKS = {"chat", "ask", "review", "fix", "implement", "verify", "document", "refine", "thread"}

# Playbooks whose turns run in IMPLEMENT mode (write sandbox + write gate).
# `thread` is write-CAPABLE but phase-gated: its finalizer runs the write
# gate only once the Thread has left the refining phase (unified dev flow
# rev2 — the phase gate lives in the platform, not the prompt).
WRITE_PLAYBOOKS = {"fix", "implement", "thread"}

# Analyze playbooks whose turns leave a "last investigated" stamp.
CHECKED_PLAYBOOKS = {"ask", "review", "verify", "document", "refine", "thread"}


async def on_turn_complete(run: Run) -> None:
    """Fire the playbook's completion hook after a turn. Never raises."""
    playbook = (run.playbook or "").strip()
    try:
        if playbook == "fix":
            from domains.delivery.services.fix_run_service import finalize_fix_run

            await finalize_fix_run(run)
        elif playbook == "implement":
            from domains.delivery.services.implement_run_service import (
                finalize_implement_run,
            )

            await finalize_implement_run(run)
        elif playbook == "verify":
            from domains.delivery.services.verification_run_service import (
                finalize_verification_run,
            )

            await finalize_verification_run(run)
        elif playbook == "thread":
            from domains.threads.services.thread_run import finalize_thread_run

            await finalize_thread_run(run)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"playbook hook failed for run {run.uid} ({run.playbook}): "
            f"{type(exc).__name__}: {exc}",
            extra={"tag": "playbooks"},
        )
    if playbook in CHECKED_PLAYBOOKS:
        try:
            from domains.checked.services.checked_service import record_for_run

            stamps = await record_for_run(run_uid=run.uid)
            if stamps:
                run.output_refs = list(
                    {*(run.output_refs or []), *(f"checked:{c.uid}" for c in stamps)}
                )
                await run.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"checked stamp failed for run {run.uid}: {type(exc).__name__}: {exc}",
                extra={"tag": "playbooks"},
            )
    if playbook == "review":
        try:
            await _continue_review_chain(run)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"review chain failed for run {run.uid}: {type(exc).__name__}: {exc}",
                extra={"tag": "playbooks"},
            )

    # Deep-scan runs author an Analysis (keyed by source_run_uid). Mark it
    # complete when the turn ends so a killed/forgetful run still finalizes.
    try:
        from domains.analysis.services.analysis_service import (
            finalize_analysis_for_run,
        )

        await finalize_analysis_for_run(run.uid)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"analysis finalize failed for run {run.uid}: {type(exc).__name__}: {exc}",
            extra={"tag": "playbooks"},
        )


async def _continue_review_chain(review_run: Run) -> None:
    """After a review turn: dispatch the skeptic pass when the fresh verdict
    is pending verification, else continue straight to the auto-fix chain
    (fix_run_service.maybe_auto_fix_for_pr — which itself skips while a
    verification is pending)."""
    if (review_run.status or "") != "awaiting_input":
        return
    pr_uid = (review_run.linked_pr_uid or "").strip()
    if not pr_uid:
        return
    from fastapi import HTTPException

    from domains.delivery.models import PullRequest
    from domains.delivery.services.fix_run_service import maybe_auto_fix_for_pr
    from domains.delivery.services.pull_request_service import latest_verdict_for
    from domains.delivery.services.verification_run_service import (
        trigger_verification_run,
    )

    # Lazy: lifecycle imports this module at import time.
    from domains.runs.services.lifecycle import LifecycleError

    pr = await PullRequest.nodes.get_or_none(uid=pr_uid)
    if pr is None:
        return
    verdict = await latest_verdict_for(pr.uid, head_sha=pr.head_sha or "")
    if (
        verdict is not None
        and (verdict.verification_status or "") == "pending"
        and not (verdict.verification_run_uid or "")
    ):
        try:
            run = await trigger_verification_run(pr, verdict)
            logger.info(
                f"verification run {run.uid} dispatched for PR #{pr.github_number} "
                f"after review {review_run.uid}",
                extra={"tag": "playbooks"},
            )
            return  # verification finalizer continues the chain
        except (HTTPException, LifecycleError) as exc:
            # Guards said no (stale verdict, run in flight) or the lifecycle
            # refused the dispatch (kill switch, no provider, unknown policy).
            # A verdict left `pending` would wedge the fix chain forever (its
            # guard skips pending verdicts) — fail open: mark verification
            # failed and let the chain proceed off the original verdict.
            verdict.verification_status = "failed"
            await verdict.save()
            detail = getattr(exc, "detail", None) or str(exc)
            logger.info(
                f"verification skipped for PR #{pr.github_number}: {detail}",
                extra={"tag": "playbooks"},
            )
    await maybe_auto_fix_for_pr(pr_uid, after_run_uid=review_run.uid)
