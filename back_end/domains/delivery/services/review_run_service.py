"""Review-run — read-only PR review ending in a SHA-bound verdict (§6).

V3: a review is a Run with playbook=review whose first-turn intent instructs
the executor to review the PR diff, file findings through the platform
tools, bind them to the PR, verify fixed claims, and finish with
submit_verdict at the exact head SHA it inspected. Quota exhaustion or an
unavailable head never fakes a verdict — the fallback is needs_human.
"""

from __future__ import annotations

from domains.delivery.models import PullRequest
from domains.delivery.services.pull_request_service import (
    latest_verdict_for,
    publish_review_status,
    review_status_for,
)
from domains.delivery.services.resolution_service import ensure_merge_policy
from domains.delivery.services.run_dispatch import dispatch_serialized
from domains.runs.models import Run
from domains.runs.schemas import (
    Executor,
    Effort,
    RunTrigger,
)
from domains.runs.services.lifecycle import trigger_run
from domains.repositories.models import Repository
from domains.repositories.services.workflow import (
    guidance_section,
    stage_depth,
    stage_prompt_body,
)
from domains.run_policies.services.effort import ensure_policy_for_effort
from infrastructure.audit import write_audit


# Depth → seeded variant guidance (opensweep://library/<slug>). "normal" uses the
# repo's configured review-stage prompt instead.
REVIEW_DEPTH_VARIANTS = {"quick": "review-quick-gate", "deep": "review-adversarial"}

_DEPTHS = ("quick", "normal", "deep")

def depth_block(depth: str, max_findings: int | None = None) -> str:
    """The intent's budget/stance paragraph. `max_findings` is the numeric
    knob: it overrides quick's default cap of 5 and puts a cap on
    normal/deep, which are otherwise uncapped."""
    if depth == "quick":
        cap = max_findings or 5
        return (
            f"Depth: QUICK — precision over recall. File at most {cap} findings, only issues\n"
            "you would block the merge over, at confidence you would defend to the author\n"
            "(≥ 0.8). An empty review is a valid outcome; do not pad it."
        )
    budget = (
        f"File at most {max_findings} findings — rank by severity × confidence and file\n"
        "the clearest, highest-impact ones first."
        if max_findings
        else "No hard finding cap; file every issue you can defend with concrete evidence."
    )
    if depth == "deep":
        return (
            "Depth: DEEP — exhaustive review. Work lens by lens: correctness, security,\n"
            "API/compatibility, performance, tests, maintainability. Where your executor\n"
            "supports subagents, delegate one lens per subagent and merge their results.\n"
            f"{budget} Note in the verdict any lens you did not check."
        )
    return f"Depth: NORMAL — {budget} Skip style-only observations."


def build_review_intent(
    pr: PullRequest,
    blocking_policy: dict,
    *,
    guidance: str | None = None,
    depth: str = "normal",
    prior_verdict_sha: str = "",
    max_findings: int | None = None,
) -> str:
    ref = f"repository_uid={pr.repository_uid} github_number={pr.github_number}"
    if prior_verdict_sha:
        scope = (
            f"3. Incremental scope: a prior verdict exists at sha `{prior_verdict_sha}`. First run\n"
            f"   `git cat-file -e {prior_verdict_sha}` — if the commit IS present, review\n"
            f"   `git diff {prior_verdict_sha}...{pr.head_ref}` for problems introduced since that\n"
            f"   verdict. If it is NOT present (shallow clone), fall back to the full scope\n"
            f"   `git diff {pr.base_ref}...{pr.head_ref}` and say so in the verdict notes.\n"
        )
        ledger_recheck = (
            "   - Re-check EVERY resolution in state open/in-fix/fixed/reopened against the\n"
            "     current head, not just `fixed` ones — the incremental diff hides regressions\n"
            "     in previously-reviewed areas; the ledger is your memory of them.\n"
        )
    else:
        scope = f"3. Review scope: `git diff {pr.base_ref}...{pr.head_ref}` — the PR's changes only.\n"
        ledger_recheck = ""
    return (
        f"Review pull request #{pr.github_number} (\"{pr.title}\") and finish with a verdict.\n"
        "\n"
        f"## Depth\n{depth_block(depth if depth in _DEPTHS else 'normal', max_findings)}\n"
        "\n"
        "## Setup\n"
        f"1. `git checkout {pr.head_ref}` (branch may already be checked out).\n"
        f"2. Confirm `git rev-parse HEAD` starts with `{pr.head_sha[:12]}`.\n"
        f"   If the branch or commit is unavailable in this sandbox, SKIP the review and submit\n"
        f"   a verdict of `needs_human` at sha `{pr.head_sha}` explaining why. Never review a\n"
        "   different commit and never guess.\n"
        + scope
        + "\n"
        "## Existing ledger state\n"
        "Check `opensweep_platform_list_comments` for human instructions on this PR's findings"
        " before deciding — a maintainer's comment outranks your own judgment.\n"
        f"4. Call `opensweep_platform_list_pr_resolutions` ({ref}) to see already-tracked findings.\n"
        "   - For each resolution in state `fixed`: check whether the original problem is truly\n"
        "     gone at this commit. If yes, call `opensweep_platform_verify_resolution` with this\n"
        "     head sha. If it is NOT fixed, leave it (an unverified fix keeps blocking) and\n"
        "     mention it in the verdict summary.\n"
        + ledger_recheck
        + "   - Do NOT re-file findings that already have a resolution (open, deferred, or waived).\n"
        "\n"
        "## Review\n"
        "5. Review the diff for correctness, security, tests, and maintainability. For every NEW\n"
        "   problem: file it with `opensweep_platform_create_finding` (concrete evidence, affected\n"
        f"   paths, severity), then bind it with `opensweep_platform_bind_finding_to_pr` ({ref},\n"
        f"   introduced_at_sha={pr.head_sha}).\n"
        f"6. Blocking policy (severities at/above threshold block merge): {blocking_policy}.\n"
        "   Count how many of YOUR new findings are blocking under this policy.\n"
        "\n"
        "## Verdict (mandatory last step)\n"
        f"7. Call `opensweep_platform_submit_verdict` ({ref}) with sha=`{pr.head_sha}` and:\n"
        "   - `approve` — zero new blocking findings AND no unverified fixed claims remain.\n"
        "   - `request_changes` — you filed one or more new blocking findings.\n"
        "   - `needs_human` — you could not complete the review (wrong commit, unreadable\n"
        "     diff, out-of-scope judgment call). Explain in ac_results notes.\n"
        "   Set `new_blocking_findings` to your blocking count. A verdict at any other sha is\n"
        "   useless — the convergence predicate discards stale verdicts.\n"
        "\n"
        "Do not modify any file. This is a read-only review."
        + guidance_section("review", guidance)
    )


async def _resolve_depth(
    pr: PullRequest, depth: Effort | None, trigger: RunTrigger
) -> Effort:
    """Explicit choice wins; auto (event) reviews use the repo's configured
    stage depth; everything else defaults to normal."""
    if depth is not None:
        return depth
    if trigger == RunTrigger.EVENT:
        return Effort(await stage_depth(pr.repository_uid, "review"))
    return Effort.NORMAL


async def _resolve_guidance(repository_uid: str, depth: Effort) -> str | None:
    """quick/deep use their seeded variant; normal (or a deleted/disabled
    variant) falls back to the repo's configured review-stage prompt."""
    from domains.agents.services.seed_variants import variant_prompt_body

    slug = REVIEW_DEPTH_VARIANTS.get(depth.value)
    if slug:
        body = await variant_prompt_body(slug)
        if body:
            return body
    return await stage_prompt_body(repository_uid, "review")


async def trigger_review_run(
    pr: PullRequest,
    *,
    triggered_by: str = "",
    trigger: RunTrigger = RunTrigger.MANUAL,
    depth: Effort | None = None,
    force_full: bool = False,
    max_findings: int | None = None,
):
    """Dispatch a review run for the PR (playbook=review, V3 §3 — no
    config object)."""

    async def _dispatch() -> Run:
        repo = await Repository.nodes.get_or_none(uid=pr.repository_uid)
        policy = await ensure_merge_policy(pr.repository_uid)
        resolved_depth = await _resolve_depth(pr, depth, trigger)
        run_policy = await ensure_policy_for_effort(resolved_depth)
        guidance = await _resolve_guidance(pr.repository_uid, resolved_depth)

        # Incremental re-review (§D): a prior verdict at an older sha narrows the
        # scope to what changed since. Deep and forced reviews stay full-scope.
        prior_verdict_sha = ""
        if not force_full and resolved_depth != Effort.DEEP:
            prior = await latest_verdict_for(pr.uid)
            if prior is not None and prior.sha and prior.sha != pr.head_sha:
                prior_verdict_sha = prior.sha

        await write_audit(
            kind="review_run.requested",
            subject_uid=pr.uid,
            subject_type="PullRequest",
            actor_uid=triggered_by,
            payload={
                "head_sha": pr.head_sha,
                "depth": resolved_depth.value,
                "incremental_from": prior_verdict_sha,
            },
        )
        # Org-agent-overlays composition: framing header + platform review
        # instructions (org overlay applied) + repo review guidance stack
        # AROUND the structural review contract (which lands in the scope
        # slot and can never be displaced by an overlay).
        from domains.agents.services.composition import compose_agent_intent

        composed = await compose_agent_intent(
            repository_uid=pr.repository_uid,
            agent_key="review",
            stage="review",
            repo_guidance=guidance or "",
            structural=build_review_intent(
                pr,
                dict(policy.blocking or {}),
                depth=resolved_depth.value,
                prior_verdict_sha=prior_verdict_sha,
                max_findings=max_findings,
            ),
        )
        run = await trigger_run(
            repository_uid=pr.repository_uid,
            intent=composed.text,
            playbook="review",
            title=f"Review PR #{pr.github_number}: {(pr.title or '')[:70]}",
            target={
                "pull_request_uid": pr.uid,
                "github_number": int(pr.github_number),
                "head_sha": pr.head_sha,
                "head_ref": pr.head_ref,
                "base_ref": pr.base_ref,
                "depth": resolved_depth.value,
                "prior_verdict_sha": prior_verdict_sha,
                "max_findings": max_findings or 0,
            },
            linked_pr_uid=pr.uid,
            executor=Executor.CLAUDE_CODE,
            run_policy_uid=run_policy.uid,
            trigger=trigger,
            triggered_by=triggered_by,
        )
        if repo is not None:
            gh_state, description = review_status_for("", 0, depth=resolved_depth.value)
            await publish_review_status(repo, pr, state=gh_state, description=description)
        return run

    # In-flight guard: no second review on this PR (two reviews double-file
    # findings). Fix/verify/chat runs may overlap — reviews are read-only and
    # verdicts are SHA-bound. Serialized per PR so two concurrent dispatches
    # can't both pass the guard.
    return await dispatch_serialized(
        target_uid=pr.uid,
        playbook="review",
        conflict_message="a review run is already in progress for this PR",
        active_filter={"pull_request_uid": pr.uid},
        dispatch=_dispatch,
    )
