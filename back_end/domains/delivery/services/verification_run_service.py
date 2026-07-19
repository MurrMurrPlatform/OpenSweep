"""Verification run — the skeptic pass challenging a blocking review verdict (§A).

A review verdict of request_changes with new blocking findings is provisional
when the repo has workflow.verify.auto on: a read-only verification run is
dispatched whose ONLY job is to refute each finding at the same head sha.
Findings it affirmatively refutes are dismissed (resolution state `refuted`,
non-blocking); everything else stays confirmed — a finding is never dismissed
without evidence against it. The platform then supersedes the original
verdict with an adjusted one at the SAME sha, counting only survivors, and
the auto-fix chain proceeds off that.

Failure posture: fail closed for merge safety (missing/failed reports =
confirmed), fail open for pipeline progress (the original verdict stays
operative and auto-fix still chains).
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import HTTPException

from domains.delivery.models import (
    FindingVerification,
    PullRequest,
    Verdict,
    verification_key,
)
from domains.delivery.schemas import ACResult, SubmitVerdictRequest, VerdictResult
from domains.delivery.services.convergence import severity_blocks
from domains.delivery.services.pull_request_service import (
    PullRequestService,
    publish_review_status,
    review_status_for,
)
from domains.delivery.services.resolution_service import ensure_merge_policy
from domains.delivery.services.run_dispatch import dispatch_serialized
from domains.findings.models import Finding
from domains.runs.models import Run
from domains.runs.schemas import Executor, Effort, RunTrigger
from domains.runs.services.lifecycle import trigger_run
from domains.repositories.models import Repository
from domains.repositories.services.workflow import guidance_section, stage_prompt_body
from domains.run_policies.services.effort import ensure_policy_for_effort
from infrastructure.audit import write_audit
from logging_config import logger


def build_verification_intent(
    pr: PullRequest,
    verdict: Verdict,
    findings: list[dict],
    *,
    guidance: str | None = None,
) -> str:
    """First-turn prompt for the skeptic run — pure, unit-testable.

    findings: [{finding_uid, resolution_uid, title, severity, evidence,
    affected_paths}]."""
    ref = f"repository_uid={pr.repository_uid} github_number={pr.github_number}"
    blocks = []
    for f in findings:
        paths = ", ".join(f.get("affected_paths") or []) or "(none listed)"
        blocks.append(
            f"### Finding `{f['finding_uid']}` — {f.get('title') or '(untitled)'}\n"
            f"- severity: {f.get('severity') or 'medium'}\n"
            f"- resolution_uid: {f.get('resolution_uid') or ''}\n"
            f"- affected paths: {paths}\n"
            f"- evidence: {f.get('evidence') or {}}"
        )
    findings_section = "\n\n".join(blocks)
    return (
        f"Verify (by trying to REFUTE) the {len(findings)} blocking finding(s) a review filed\n"
        f"against pull request #{pr.github_number} (\"{pr.title}\").\n"
        "\n"
        "## Setup\n"
        f"1. `git checkout {pr.head_ref}` (branch may already be checked out).\n"
        f"2. Confirm `git rev-parse HEAD` starts with `{verdict.sha[:12]}`.\n"
        "   If the branch or commit is unavailable in this sandbox, report EVERY finding\n"
        "   below as `needs-human` and stop — never judge a different commit.\n"
        "\n"
        "## Your stance\n"
        "You are the skeptic. For each finding, actively try to show the claimed failure\n"
        "CANNOT occur at this commit:\n"
        "- `refuted` — only when you can cite file:line evidence at THIS commit that the\n"
        "  failure mechanism does not exist (guard already present, path unreachable,\n"
        "  claim contradicts the actual code, framework already mitigates it).\n"
        "- `confirmed` — the failure mechanism is real as described (or worse).\n"
        "- `needs-human` — you cannot determine it either way. Never guess.\n"
        "\n"
        "## Findings under verification\n"
        f"{findings_section}\n"
        "\n"
        "## Reporting (mandatory)\n"
        f"For EACH finding above, call `opensweep_platform_submit_finding_verification` ({ref},\n"
        f"verdict_uid=`{verdict.uid}`, sha=`{verdict.sha}`) with the finding_uid, your result,\n"
        "and reasoning citing the evidence you checked. Findings you skip are treated as\n"
        "confirmed — silence never dismisses a finding.\n"
        "\n"
        "Do not modify any file. Do not submit a verdict — the platform adjusts it from\n"
        "your reports. Do not file new findings unless you discover a clearly NEW defect\n"
        "unrelated to the ones above."
        + guidance_section("verify", guidance)
    )


def adjusted_verdict_outcome(
    confirmed: list[dict], blocking_policy: dict
) -> tuple[str, int]:
    """(result, blocking_count) after verification — pure.

    confirmed: [{severity, tags}] of the findings that survived."""
    blocking = sum(
        1
        for f in confirmed
        if severity_blocks(
            (f.get("severity") or "medium"), list(f.get("tags") or []), blocking_policy
        )
    )
    return ("request_changes" if blocking else "approve"), blocking


async def trigger_verification_run(
    pr: PullRequest,
    verdict: Verdict,
    *,
    triggered_by: str = "system",
    trigger: RunTrigger = RunTrigger.EVENT,
):
    """Dispatch the skeptic run for a pending verdict (playbook=verify)."""
    if (verdict.verification_status or "") != "pending":
        raise HTTPException(status_code=409, detail="verdict is not pending verification")
    if verdict.verification_run_uid:
        raise HTTPException(
            status_code=409, detail="a verification run was already dispatched for this verdict"
        )
    if (verdict.sha or "") != (pr.head_sha or ""):
        raise HTTPException(status_code=409, detail="verdict is stale (head moved) — nothing to verify")

    async def _dispatch() -> Run:
        findings = []
        from domains.delivery.models import FindingResolution, resolution_key

        for finding_uid in list(verdict.finding_uids or []):
            finding = await Finding.nodes.get_or_none(uid=finding_uid)
            if finding is None:
                continue
            resolution = await FindingResolution.nodes.get_or_none(
                resolution_key=resolution_key(finding_uid, pr.uid)
            )
            findings.append(
                {
                    "finding_uid": finding_uid,
                    "resolution_uid": resolution.uid if resolution else "",
                    "title": finding.title or "",
                    "severity": finding.severity or "medium",
                    "evidence": dict(finding.evidence or {}),
                    "affected_paths": list(finding.affected_paths or []),
                }
            )
        if not findings:
            raise HTTPException(status_code=409, detail="verdict has no findings to verify")

        run_policy = await ensure_policy_for_effort(Effort.NORMAL)
        guidance = await stage_prompt_body(pr.repository_uid, "verify")

        # Org-agent-overlays composition: header + platform verify
        # instructions (org overlay applied) + repo verify guidance stack
        # around the structural skeptic contract.
        from domains.agents.services.composition import compose_agent_intent

        composed = await compose_agent_intent(
            repository_uid=pr.repository_uid,
            agent_key="verify",
            stage="verify",
            repo_guidance=guidance or "",
            structural=build_verification_intent(pr, verdict, findings),
        )
        run = await trigger_run(
            repository_uid=pr.repository_uid,
            intent=composed.text,
            playbook="verify",
            title=f"Verify {len(findings)} finding(s) on PR #{pr.github_number}",
            target={
                "pull_request_uid": pr.uid,
                "verdict_uid": verdict.uid,
                "github_number": int(pr.github_number),
                "head_sha": pr.head_sha,
                "head_ref": pr.head_ref,
                "base_ref": pr.base_ref,
                "finding_uids": [f["finding_uid"] for f in findings],
            },
            linked_pr_uid=pr.uid,
            executor=Executor.CLAUDE_CODE,
            run_policy_uid=run_policy.uid,
            trigger=trigger,
            triggered_by=triggered_by,
        )
        verdict.verification_run_uid = run.uid
        await verdict.save()
        await write_audit(
            kind="verification_run.dispatched",
            subject_uid=verdict.uid,
            subject_type="Verdict",
            payload={"run_uid": run.uid, "pr": pr.pr_key, "findings": len(findings)},
        )
        return run

    # In-flight guard + per-PR serialization: two concurrent dispatches for
    # the same PR can't both pass the guard.
    return await dispatch_serialized(
        target_uid=pr.uid,
        playbook="verify",
        conflict_message="a verification run is already in progress for this PR",
        active_filter={"pull_request_uid": pr.uid},
        dispatch=_dispatch,
    )


async def submit_finding_verification(
    *,
    run_uid: str,
    verdict_uid: str,
    pull_request_uid: str,
    repository_uid: str,
    finding_uid: str,
    sha: str,
    result: str,
    reasoning: str,
) -> FindingVerification:
    """Upsert one judgment, keyed (run_uid, finding_uid) — re-calls update."""
    key = verification_key(run_uid, finding_uid)
    row = await FindingVerification.nodes.get_or_none(verification_key=key)
    if row is None:
        row = FindingVerification(
            uid=uuid4().hex,
            pull_request_uid=pull_request_uid,
            repository_uid=repository_uid,
            verdict_uid=verdict_uid,
            finding_uid=finding_uid,
            run_uid=run_uid,
            verification_key=key,
        )
    row.sha = sha
    row.result = result
    row.reasoning = reasoning
    await row.save()
    return row


async def finalize_verification_run(run: Run) -> None:
    """Playbook hook: fold the run's judgments into the ledger and supersede
    the pending verdict with an adjusted one. Never raises past the caller's
    hook guard."""
    verdict_uid = str((run.target or {}).get("verdict_uid") or "")
    if not verdict_uid:
        return  # single-finding verify flow (api/v1/findings.py) — not ours
    verdict = await Verdict.nodes.get_or_none(uid=verdict_uid)
    if verdict is None:
        return
    if (verdict.verification_status or "") != "pending":
        return
    if (verdict.verification_run_uid or "") != run.uid:
        return
    pr = await PullRequest.nodes.get_or_none(uid=verdict.pull_request_uid)
    if pr is None:
        return
    repo = await Repository.nodes.get_or_none(uid=pr.repository_uid)

    # Pipeline fail-open: a run that never completed leaves the original
    # verdict operative (all findings confirmed) and the fix chain proceeds.
    if (run.status or "") != "awaiting_input":
        verdict.verification_status = "failed"
        await verdict.save()
        await write_audit(
            kind="verification_run.failed",
            subject_uid=verdict.uid,
            subject_type="Verdict",
            payload={"run_uid": run.uid, "run_status": run.status or ""},
        )
        if repo is not None:
            gh_state, description = review_status_for(
                verdict.result or "", int(verdict.new_blocking_findings or 0), "failed"
            )
            await publish_review_status(repo, pr, state=gh_state, description=description)
        await _chain_auto_fix(pr.uid, after_run_uid=run.uid)
        return

    reports = {
        row.finding_uid: row
        for row in await FindingVerification.nodes.filter(run_uid=run.uid)
    }
    confirmed: list[dict] = []
    refuted_uids: list[str] = []
    for finding_uid in list(verdict.finding_uids or []):
        report = reports.get(finding_uid)
        result = (report.result if report else "") or "confirmed"
        if result == "refuted":
            await _dismiss_refuted(
                finding_uid, pr.uid, run.uid, report.reasoning if report else ""
            )
            refuted_uids.append(finding_uid)
            continue
        if result == "needs-human":
            await write_audit(
                kind="verification.needs_human",
                subject_uid=finding_uid,
                subject_type="Finding",
                payload={"run_uid": run.uid, "pr": pr.pr_key},
            )
        finding = await Finding.nodes.get_or_none(uid=finding_uid)
        confirmed.append(
            {
                "finding_uid": finding_uid,
                "severity": (finding.severity if finding else "medium") or "medium",
                "tags": list(finding.tags or []) if finding else [],
            }
        )

    # Head moved mid-verification: the refutations are finding-level truth and
    # stay recorded, but an adjusted verdict at the old sha would be stale on
    # arrival — skip it and the fix chain (the fresh head gets its own review).
    if (pr.head_sha or "") != (verdict.sha or ""):
        verdict.verification_status = "superseded"
        await verdict.save()
        await write_audit(
            kind="verification_run.stale_head",
            subject_uid=verdict.uid,
            subject_type="Verdict",
            payload={"run_uid": run.uid, "verdict_sha": verdict.sha, "head_sha": pr.head_sha},
        )
        return

    policy = await ensure_merge_policy(pr.repository_uid)
    result, blocking = adjusted_verdict_outcome(confirmed, dict(policy.blocking or {}))
    note = (
        f"verification run {run.uid}: {len(refuted_uids)} refuted, "
        f"{len(confirmed)} confirmed of {len(verdict.finding_uids or [])}"
    )
    adjusted_dto = await PullRequestService().submit_verdict(
        pr.uid,
        SubmitVerdictRequest(
            sha=verdict.sha,
            result=VerdictResult(result),
            new_blocking_findings=blocking,
            finding_uids=[f["finding_uid"] for f in confirmed],
            ac_results=[ACResult(criterion="verification", result="pass", note=note)],
            source_run_uid=run.uid,
            executor="verification",
        ),
        actor_uid=run.uid,
    )
    adjusted = await Verdict.nodes.get_or_none(uid=adjusted_dto.uid)
    if adjusted is not None:
        adjusted.verification_status = "adjusted"
        await adjusted.save()
    verdict.verification_status = "superseded"
    await verdict.save()
    await write_audit(
        kind="verification_run.adjusted",
        subject_uid=verdict.uid,
        subject_type="Verdict",
        payload={
            "run_uid": run.uid,
            "adjusted_verdict_uid": adjusted_dto.uid,
            "result": result,
            "refuted": refuted_uids,
            "confirmed_blocking": blocking,
        },
    )
    await _chain_auto_fix(pr.uid, after_run_uid=run.uid)


async def _dismiss_refuted(
    finding_uid: str, pull_request_uid: str, run_uid: str, reasoning: str
) -> None:
    """Refuted = machine-disproved: Finding → dismissed (evidence trail kept),
    its PR resolution → refuted (non-blocking)."""
    from datetime import UTC, datetime

    from domains.delivery.models import FindingResolution, resolution_key
    from domains.delivery.services.resolution_service import ResolutionService
    from domains.findings.schemas import FindingStatus

    finding = await Finding.nodes.get_or_none(uid=finding_uid)
    if finding is not None:
        finding.status = FindingStatus.DISMISSED.value
        finding.evidence = {
            **(finding.evidence or {}),
            "refuted_by_run": run_uid,
            "refute_reasoning": reasoning,
        }
        finding.updated_at = datetime.now(UTC)
        await finding.save()
    resolution = await FindingResolution.nodes.get_or_none(
        resolution_key=resolution_key(finding_uid, pull_request_uid)
    )
    if resolution is not None:
        await ResolutionService().refute(resolution.uid, run_uid=run_uid, reasoning=reasoning)


async def _chain_auto_fix(pr_uid: str, *, after_run_uid: str) -> None:
    """Best-effort continuation of the review → fix chain."""
    try:
        from domains.delivery.services.fix_run_service import maybe_auto_fix_for_pr

        await maybe_auto_fix_for_pr(pr_uid, after_run_uid=after_run_uid)
    except Exception as exc:  # noqa: BLE001 — hook context, never raise
        logger.warning(
            f"auto-fix chain after verification {after_run_uid} failed: "
            f"{type(exc).__name__}: {exc}",
            extra={"tag": "delivery"},
        )
