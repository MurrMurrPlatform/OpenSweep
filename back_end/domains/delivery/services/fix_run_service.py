"""Fix-run — write-path run that resolves a PR's blocking findings (§6).

Bounded loop: `PullRequest.fix_rounds` counts DISPATCHES against
`MergePolicy.max_fix_rounds`; exhaustion → 409 ("human required" — the
Needs-You escape). Follow-up turns inside one fix run do NOT burn rounds —
iterating in the conversation is the cheap path (V3 §3). The agent fixes
findings, runs tests, commits, and claims fixes via
`opensweep_platform_attach_fix`; it never pushes. The platform gate validates
the sandbox after EVERY turn and pushes to the SAME branch (never force),
which retriggers review via webhook — plus a best-effort direct resync.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException

from domains.delivery.models import FindingResolution, PullRequest
from domains.delivery.services import write_gate
from domains.delivery.services.convergence import resolution_is_blocking
from domains.docs.services.doc_freshness import docs_watching_paths
from domains.delivery.services.resolution_service import ensure_merge_policy
from domains.delivery.services.run_dispatch import (
    DOC_UPKEEP_INTENT_SECTION,
    dispatch_serialized,
    finalize_write_run,
    require_repository,
)
from domains.execution.schemas import SandboxDTO
from domains.execution.services.sandbox_service import SandboxService
from domains.findings.models import Finding
from domains.investigations.models import Run
from domains.investigations.schemas import (
    ExecutionMode,
    Executor,
    InvestigationEffort,
    RunTrigger,
)
from domains.investigations.services.lifecycle import trigger_run
from domains.repositories.services.repository_service import repository_to_dto
from domains.repositories.services.workflow import guidance_section, stage_prompt_body
from domains.run_policies.services.effort import ensure_policy_for_effort
from infrastructure.audit import write_audit
from logging_config import logger

# Ledger states a fix run may work on ("fixed" is already claimed; verification
# belongs to review runs).
FIXABLE_RESOLUTION_STATES = {"open", "in-fix", "reopened"}


def build_fix_intent(
    pr: PullRequest, findings: list[dict], denylist: list[str], *, guidance: str | None = None
) -> str:
    """Fix-run intent. `findings` items are plain dicts (resolution_uid, title,
    severity, tags, blocking, why_it_matters, suggested_fix, evidence,
    affected_paths) so the contract is unit-testable without the DB."""
    blocks = []
    for i, f in enumerate(findings, start=1):
        paths = "\n".join(f"   - {p}" for p in (f.get("affected_paths") or [])) or "   - (none recorded)"
        blocks.append(
            f"### Finding {i}: {f.get('title', '(untitled)')}\n"
            f"- resolution_uid: `{f.get('resolution_uid', '')}`\n"
            f"- severity: {f.get('severity', 'medium')} | tags: {', '.join(f.get('tags') or []) or '(none)'} | "
            f"blocking: {'yes' if f.get('blocking') else 'no'}\n"
            f"- why it matters: {(f.get('why_it_matters') or '(not provided)').strip()}\n"
            f"- suggested fix: {(f.get('suggested_fix') or '(none — use your judgment)').strip()}\n"
            f"- evidence: {f.get('evidence') or '{}'}\n"
            f"- affected paths:\n{paths}"
        )
    findings_block = "\n\n".join(blocks)
    deny_block = "\n".join(f"   - `{p}`" for p in denylist) or "   - (none configured)"
    return (
        f"Fix the blocking findings on pull request #{pr.github_number} (\"{pr.title}\").\n"
        "\n"
        "## Working copy\n"
        f"- The PR's head branch `{pr.head_ref}` is already checked out in your current directory.\n"
        "  Never switch branches, never rebase, never rewrite existing history.\n"
        "\n"
        "## Findings to fix\n"
        "Check `opensweep_platform_list_comments` for human instructions on this PR's findings"
        " before deciding how to fix them.\n"
        "\n"
        f"{findings_block}\n"
        "\n"
        "## Rules (the platform validates your commits after the run)\n"
        "- Fix each finding minimally; run the repository's tests for the code you touched.\n"
        "- Do NOT touch any path matching these forbidden patterns (the platform blocks the push otherwise):\n"
        f"{deny_block}\n"
        "- Commit your work with clear conventional commit message(s).\n"
        "- For EVERY resolution you fixed, call `opensweep_platform_attach_fix` with its\n"
        "  `resolution_uid` and the sha of the commit that fixes it (`git rev-parse HEAD`\n"
        "  after committing). Skip resolutions you could not fix and say why in the summary.\n"
        "- DO NOT push. Never run `git push` — the platform validates and pushes this branch.\n"
        "\n"
        + DOC_UPKEEP_INTENT_SECTION
        + "\n"
        "## Finish (mandatory)\n"
        "- Call `complete_run` summarizing: commits made (sha + message), which resolutions\n"
        "  you attached fixes to, which you skipped and why, test results, and the\n"
        "  doc/memory upkeep you did (pages updated/confirmed/proposed, memories written).\n"
        + guidance_section("fix", guidance)
    )


async def trigger_fix_run(
    pr: PullRequest,
    *,
    triggered_by: str = "",
    trigger: RunTrigger = RunTrigger.MANUAL,
    finding_uids: list[str] | None = None,
) -> Run:
    """Dispatch a fix run on the PR's existing head branch."""
    if pr.state != "open":
        raise HTTPException(status_code=409, detail=f"PR is {pr.state}; only open PRs get fix runs")
    if not pr.head_ref:
        raise HTTPException(status_code=409, detail="PR has no head_ref — sync it first")
    repo = await require_repository(pr.repository_uid, require_github=True)

    # Thread-owned PR (unified dev flow rev2): review feedback goes INTO the
    # thread conversation — same agent, full context — instead of a cold fix
    # run. Rounds stay bounded on the PR exactly like dispatched fix runs.
    thread_run = await _thread_conversation_for_pr(pr)
    if thread_run is not None:
        return await _message_fix_to_thread(
            pr, thread_run, finding_uids, triggered_by=triggered_by
        )

    async def _dispatch() -> Run:
        policy = await ensure_merge_policy(pr.repository_uid)
        if write_gate.fix_rounds_exhausted(int(pr.fix_rounds or 0), int(policy.max_fix_rounds or 0)):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"fix rounds exhausted ({pr.fix_rounds}/{policy.max_fix_rounds}) — "
                    "human required"
                ),
            )

        findings = await _collect_fixable_findings(pr, policy, finding_uids)
        if not findings:
            raise HTTPException(
                status_code=409,
                detail="no open/reopened finding resolutions to fix on this PR",
            )

        # Pre-load the pages documenting the code these findings touch, so the
        # briefing inlines them verbatim (agent doesn't have to fetch first).
        # Best-effort — a resolution failure must not block the fix dispatch.
        target_doc_uids = await _docs_for_findings(pr.repository_uid, findings)

        # The round this dispatch will become. Persisted before dispatch and
        # refunded if the dispatch (or its background sandbox prep) never reaches
        # the agent — a failed dispatch must not burn a round.
        next_round = int(pr.fix_rounds or 0) + 1

        denylist = write_gate.effective_denylist(policy)
        run_policy = await ensure_policy_for_effort(InvestigationEffort.NORMAL)

        # The slow git clone is deferred into the run's background pipeline via
        # this factory — the dispatch request returns as soon as the queued run
        # row exists, so the UI flips to "in progress" immediately.
        repo_dto = repository_to_dto(repo)

        async def _make_sandbox() -> SandboxDTO:
            return await SandboxService().create_for_write(
                repository=repo_dto,
                agent_run_uid=pr.uid,
                work_branch=pr.head_ref,
                checkout_existing=True,  # fix runs continue the EXISTING PR branch
            )

        # Burn the round BEFORE dispatch so the counter is already consistent when
        # the background pipeline (and its finalizer) starts; refunded below on a
        # synchronous dispatch failure, and by the finalizer when sandbox prep
        # fails (the agent never ran — that must not cost a round, §6).
        pr.fix_rounds = next_round
        pr.fix_rounds_exhausted = write_gate.fix_rounds_exhausted(
            next_round, int(policy.max_fix_rounds or 0)
        )
        pr.updated_at = datetime.now(UTC)
        await pr.save()

        # Org-agent-overlays composition: header + platform fix instructions
        # (org overlay applied) + repo fix guidance stack around the
        # structural fix contract (findings, denylist, write-gate rules).
        from domains.agent_overlays.services.composition import compose_playbook_intent

        guidance = await stage_prompt_body(pr.repository_uid, "fix")
        composed = await compose_playbook_intent(
            repository_uid=pr.repository_uid,
            playbook="fix",
            stage="fix",
            repo_guidance=guidance or "",
            structural=build_fix_intent(pr, findings, denylist),
        )
        try:
            run = await trigger_run(
                repository_uid=pr.repository_uid,
                intent=composed.text,
                playbook="fix",
                title=f"Fix PR #{pr.github_number}: {(pr.title or '')[:70]}",
                target={
                    "pull_request_uid": pr.uid,
                    "github_number": int(pr.github_number),
                    "head_ref": pr.head_ref,
                    "base_ref": pr.base_ref,
                    "fix_round": next_round,
                    "resolution_uids": [f["resolution_uid"] for f in findings],
                    "doc_uids": target_doc_uids,
                },
                linked_pr_uid=pr.uid,
                executor=Executor.CLAUDE_CODE,
                execution_mode=ExecutionMode.IMPLEMENT,
                run_policy_uid=run_policy.uid,
                trigger=trigger,
                triggered_by=triggered_by,
                sandbox_factory=_make_sandbox,
            )
        except Exception:
            await _refund_fix_round(pr.uid, reason="dispatch failed")
            raise
        await write_audit(
            kind="fix_run.dispatched",
            subject_uid=pr.uid,
            subject_type="PullRequest",
            actor_uid=triggered_by,
            payload={
                "fix_round": pr.fix_rounds,
                "max_fix_rounds": int(policy.max_fix_rounds or 0),
                "resolution_uids": [f["resolution_uid"] for f in findings],
                "run_uid": run.uid,
            },
        )

        # Thread follow-through: the conversation continues with the fixer.
        from domains.threads.services.hooks import note_fix_run_for_pr

        await note_fix_run_for_pr(pr.uid, run)

        return run

    # In-flight guard: one WRITE run per PR at a time — two writers race over
    # the same branch. Read-only runs (review, verify, chat) may overlap;
    # their outputs are SHA-bound. Serialized per PR so two concurrent
    # dispatches can't both pass the guard (#TOCTOU).
    return await dispatch_serialized(
        target_uid=pr.uid,
        playbook="fix",
        conflict_message="a write run is already in progress for this PR",
        active_filter={"pull_request_uid": pr.uid},
        dispatch=_dispatch,
    )


async def maybe_auto_fix_for_pr(pr_uid: str, *, after_run_uid: str = "") -> Run | None:
    """Workflow fix stage on auto: dispatch a fix run when the PR's fresh
    verdict is request_changes AND no verification is still pending on it.

    Shared continuation of the review → verify → fix chain (playbook hooks
    and the verification finalizer both call this). trigger_fix_run's own
    guards bound the loop (fix rounds, one in-flight run per PR, open PR
    only) — a guard rejection is normal control flow, not an error."""
    from domains.delivery.services.pull_request_service import latest_verdict_for
    from domains.repositories.services.workflow import stage_auto

    if not pr_uid:
        return None
    pr = await PullRequest.nodes.get_or_none(uid=pr_uid)
    if pr is None or pr.state != "open":
        return None
    # Draft = not in the review→fix loop yet — EXCEPT for thread-owned PRs:
    # their findings route into the live conversation (a message turn, not a
    # cold run), and the thread must keep iterating even when the ready
    # signal's un-draft failed provider-side.
    if pr.draft and await _thread_conversation_for_pr(pr) is None:
        return None
    if not await stage_auto(pr.repository_uid, "fix"):
        return None
    verdict = await latest_verdict_for(pr.uid, head_sha=pr.head_sha or "")
    if verdict is None or (verdict.result or "") != "request_changes":
        return None
    if (verdict.sha or "") != (pr.head_sha or ""):
        return None  # stale verdict — never chain off it
    if (verdict.verification_status or "") == "pending":
        return None  # skeptic pass owns the chain until it lands
    try:
        run = await trigger_fix_run(pr, triggered_by="auto-workflow", trigger=RunTrigger.EVENT)
    except HTTPException as exc:
        # 409s are the guards doing their job (rounds exhausted, run in
        # flight, nothing fixable) — log at info, not warning.
        logger.info(
            f"auto-fix skipped for PR #{pr.github_number}: {exc.detail}",
            extra={"tag": "delivery"},
        )
        return None
    logger.info(
        f"auto-fix run {run.uid} dispatched for PR #{pr.github_number}"
        + (f" after run {after_run_uid}" if after_run_uid else ""),
        extra={"tag": "delivery"},
    )
    return run


async def _thread_conversation_for_pr(pr: PullRequest) -> Run | None:
    """The PR's thread-playbook conversation, when one is live (rev2)."""
    from domains.threads.models import Thread

    try:
        threads = await Thread.nodes.filter(pr_uid=pr.uid)
    except Exception:  # noqa: BLE001 — never block fix dispatch on thread lookup
        return None
    for thread in threads:
        if thread.phase != "in_review" or not thread.active_run_uid:
            continue
        run = await Run.nodes.get_or_none(uid=thread.active_run_uid)
        if run is not None and run.playbook == "thread":
            return run
    return None


async def _message_fix_to_thread(
    pr: PullRequest,
    run: Run,
    finding_uids: list[str] | None,
    *,
    triggered_by: str = "",
) -> Run:
    """Deliver review findings as a message turn of the thread conversation.
    Burns a fix round (same bound as dispatched fix runs).

    Deliverability is checked BEFORE the round is burned — a mid-turn run
    would silently lose the message after irreversibly spending the round."""
    from domains.threads.services.thread_run import (
        build_fix_message,
        run_accepts_message,
        send_message_turn,
    )

    if not await run_accepts_message(run.uid):
        raise HTTPException(
            status_code=409,
            detail=(
                "the thread conversation is mid-turn — the fix round was NOT "
                "spent; retry when the turn finishes"
            ),
        )
    policy = await ensure_merge_policy(pr.repository_uid)
    if write_gate.fix_rounds_exhausted(int(pr.fix_rounds or 0), int(policy.max_fix_rounds or 0)):
        raise HTTPException(
            status_code=409,
            detail=(
                f"fix rounds exhausted ({pr.fix_rounds}/{policy.max_fix_rounds}) — "
                "human required"
            ),
        )
    findings = await _collect_fixable_findings(pr, policy, finding_uids)
    if not findings:
        raise HTTPException(
            status_code=409,
            detail="no open/reopened finding resolutions to fix on this PR",
        )
    next_round = int(pr.fix_rounds or 0) + 1
    pr.fix_rounds = next_round
    pr.fix_rounds_exhausted = write_gate.fix_rounds_exhausted(
        next_round, int(policy.max_fix_rounds or 0)
    )
    pr.updated_at = datetime.now(UTC)
    await pr.save()

    message = build_fix_message(
        pr, findings, fix_round=next_round, max_rounds=int(policy.max_fix_rounds or 0)
    )
    send_message_turn(run.uid, message)
    await write_audit(
        kind="fix_run.messaged_thread",
        subject_uid=pr.uid,
        subject_type="PullRequest",
        actor_uid=triggered_by,
        payload={
            "fix_round": next_round,
            "max_fix_rounds": int(policy.max_fix_rounds or 0),
            "run_uid": run.uid,
            "resolution_uids": [f["resolution_uid"] for f in findings],
        },
    )
    # Thread timeline follow-through — never raises.
    from domains.threads.services.hooks import note_fix_run_for_pr

    await note_fix_run_for_pr(pr.uid, run)
    return run


async def _collect_fixable_findings(
    pr: PullRequest, policy, finding_uids: list[str] | None
) -> list[dict]:
    resolutions = await FindingResolution.nodes.filter(pull_request_uid=pr.uid)
    blocking_policy = dict(policy.blocking or {})
    wanted = set(finding_uids or [])
    out: list[dict] = []
    for r in resolutions:
        if (r.state or "open") not in FIXABLE_RESOLUTION_STATES:
            continue
        if wanted and r.finding_uid not in wanted:
            continue
        finding = await Finding.nodes.get_or_none(uid=r.finding_uid)
        severity = (finding.severity if finding else "medium") or "medium"
        tags = list(finding.tags or []) if finding else []
        out.append(
            {
                "resolution_uid": r.uid,
                "finding_uid": r.finding_uid,
                "title": (finding.title if finding else "") or f"finding {r.finding_uid[:8]}",
                "severity": severity,
                "tags": tags,
                "blocking": resolution_is_blocking(
                    state=r.state or "open",
                    severity=severity,
                    tags=tags,
                    blocking_policy=blocking_policy,
                    override=r.blocking_override or "",
                ),
                "why_it_matters": (finding.why_it_matters if finding else "") or "",
                "suggested_fix": (finding.suggested_fix if finding else "") or "",
                "evidence": dict(finding.evidence or {}) if finding else {},
                "affected_paths": list(finding.affected_paths or []) if finding else [],
            }
        )
    # Blocking findings first — if the agent runs out of budget, it fixed
    # the merge-relevant ones.
    out.sort(key=lambda f: (not f["blocking"], f["severity"]))
    return out


async def _docs_for_findings(repository_uid: str, findings: list[dict]) -> list[str]:
    """Doc uids watching the paths these findings touch — for briefing pre-load.
    Best-effort: any failure yields no pre-load (the index + read_doc cover it)."""
    try:
        paths = [p for f in findings for p in (f.get("affected_paths") or [])]
        return await docs_watching_paths(repository_uid, paths)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"doc pre-load for fix findings failed ({repository_uid}): {exc}",
            extra={"tag": "delivery"},
        )
        return []


async def _refund_fix_round(pr_uid: str, *, reason: str) -> None:
    """Give back a burned fix round (§6) — the dispatch never reached the
    agent, so it must not count against MergePolicy.max_fix_rounds."""
    pr = await PullRequest.nodes.get_or_none(uid=pr_uid)
    if pr is None:
        return
    policy = await ensure_merge_policy(pr.repository_uid)
    pr.fix_rounds = max(0, int(pr.fix_rounds or 0) - 1)
    pr.fix_rounds_exhausted = write_gate.fix_rounds_exhausted(
        int(pr.fix_rounds), int(policy.max_fix_rounds or 0)
    )
    pr.updated_at = datetime.now(UTC)
    await pr.save()
    await write_audit(
        kind="fix_run.round_refunded",
        subject_uid=pr.uid,
        subject_type="PullRequest",
        actor_uid="system",
        payload={"fix_rounds": int(pr.fix_rounds), "reason": reason[:300]},
    )


async def finalize_fix_run(run: Run) -> None:
    """Per-turn playbook hook (V3 §3): validate the NEW commits → push same
    branch. Derived entirely from the run (playbook registry re-fires it
    after every turn, including follow-ups). Refunds the fix round when
    sandbox prep failed — the agent never ran, that must not cost a round.
    """
    pr_uid = run.linked_pr_uid or str((run.target or {}).get("pull_request_uid") or "")
    if not pr_uid:
        return
    if dict(run.usage or {}).get("prep_failed"):
        await _refund_fix_round(pr_uid, reason=run.error or "sandbox prep failed")
        return
    pr = await PullRequest.nodes.get_or_none(uid=pr_uid)
    if pr is None:
        return

    async def _after_push(sandbox: SandboxDTO, result: write_gate.WriteGateResult) -> None:
        # Pushed to the SAME branch — never force. GitHub's synchronize
        # webhook takes it from here; we also resync directly (best-effort).
        await write_audit(
            kind="fix_run.pushed",
            subject_uid=pr.uid,
            subject_type="PullRequest",
            actor_uid="system",
            payload={"run_uid": run.uid, "branch": pr.head_ref, "commits": result.commits},
        )
        try:
            from domains.delivery.services.pull_request_service import PullRequestService

            await PullRequestService().sync_from_github(pr.repository_uid, int(pr.github_number))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"post-fix PR resync failed for {pr.pr_key}: {exc}", extra={"tag": "delivery"}
            )

    # base_ref = the remote head branch: only the agent's NEW commits/paths
    # are judged. A follow-up turn with zero new commits (e.g. the user asked
    # a question) is validated and blocked without a push — harmless.
    await finalize_write_run(
        run,
        audit_prefix="fix_run",
        subject_uid=pr.uid,
        subject_type="PullRequest",
        repository_uid=pr.repository_uid,
        base_ref=pr.head_ref,
        work_branch=pr.head_ref,
        on_pushed=_after_push,
    )
