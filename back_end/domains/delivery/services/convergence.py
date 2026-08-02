"""Convergence predicate — pure functions, no I/O (PLATFORM_V2_DESIGN.md §5).

A PullRequest is CONVERGED iff, all evaluated at head_sha:
  1. CI green, strictly: rollup non-empty, all concluded, zero failures.
  2. Fresh verdict: latest verdict sha == head_sha and result == approve.
  3. Clean round: that verdict raised zero new blocking findings (when the
     MergePolicy requires a clean round).
  4. Ledger clear: zero blocking-capable resolutions that the policy (or a
     human override) marks blocking. Deferred and waived never block.

Everything here takes plain data in and returns plain data out so the
predicate is unit-testable without Neo4j or GitHub.
"""

from domains.delivery.models import BLOCKING_CAPABLE_STATES
from domains.delivery.schemas import (
    CIState,
    ConvergenceCounts,
    ConvergenceState,
    VerdictResult,
)

# The shared ladder (it had three divergent copies). The merge-blocking
# threshold below is the one place severity gates behaviour, so it must not
# drift from the ladder the batch selection filters rank with.
from infrastructure.ranking import SEVERITY_ORDER

_CI_FAILURE_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
_CI_OK_CONCLUSIONS = {"success", "neutral", "skipped"}


def compute_ci_state(checks: list[dict]) -> CIState:
    """Strict rollup: an empty or pending rollup is NOT green (§5.1).

    Closes the auto-merge hole where `not any(failures)` treated an empty
    rollup as passing.
    """
    if not checks:
        return CIState.EMPTY
    if any((c.get("conclusion") or "") in _CI_FAILURE_CONCLUSIONS for c in checks):
        return CIState.RED
    if any((c.get("status") or "") != "completed" for c in checks):
        return CIState.PENDING
    if all((c.get("conclusion") or "") in _CI_OK_CONCLUSIONS for c in checks):
        return CIState.GREEN
    # Completed with an unknown conclusion — never assume green.
    return CIState.PENDING


def severity_blocks(severity: str, tags: list[str], blocking_policy: dict) -> bool:
    """True when the MergePolicy makes this severity blocking for a finding
    carrying these tags. The strictest (lowest) matching per-tag threshold
    wins; findings with no matching tag fall back to the default."""
    per_tag = blocking_policy.get("per_tag") or {}
    thresholds = [per_tag[t] for t in (tags or []) if t in per_tag]
    if thresholds:
        threshold = min(thresholds, key=lambda t: SEVERITY_ORDER.get(t, 2))
    else:
        threshold = blocking_policy.get("default") or "high"
    return SEVERITY_ORDER.get(severity, 1) >= SEVERITY_ORDER.get(threshold, 2)


def resolution_is_blocking(
    *,
    state: str,
    severity: str,
    tags: list[str],
    blocking_policy: dict,
    override: str = "",
) -> bool:
    """Blocking is computed, not stored (§4): policy decides unless a human
    override says otherwise. Terminal-good / deferred / waived states never block."""
    if state not in BLOCKING_CAPABLE_STATES:
        return False
    if override == "allow":
        return False
    if override == "block":
        return True
    return severity_blocks(severity, tags, blocking_policy)


def count_blocking_findings(findings: list[dict], blocking_policy: dict) -> int:
    """How many of these findings the MergePolicy makes blocking.

    `findings`: [{severity, tags, override}] — `override` is the human
    blocking_override from any FindingResolution already bound to the PR
    ("allow" / "block" / ""), so a reviewer's explicit call still wins.

    This is the derivation behind a verdict's `new_blocking_findings`. That
    number gates the clean-round condition (§5.3) and therefore the published
    `opensweep/converged` status, so it must not be whatever the reviewing
    model reported about its own output — the review intent asks the agent to
    "count how many of YOUR new findings are blocking", and an agent that
    answers 0 would otherwise mint a clean round for itself.
    """
    total = 0
    for f in findings:
        override = str(f.get("override") or "")
        if override == "allow":
            continue
        if override == "block":
            total += 1
            continue
        if severity_blocks(
            str(f.get("severity") or "medium"),
            list(f.get("tags") or []),
            blocking_policy,
        ):
            total += 1
    return total


def compute_convergence(
    *,
    head_sha: str,
    ci_checks: list[dict],
    latest_verdict: dict | None,
    resolutions: list[dict],
    blocking_policy: dict,
    require_clean_round: bool = True,
    pr_state: str = "open",
    draft: bool = False,
    base_is_default: bool = True,
    base_advanced_at: object = None,
) -> ConvergenceState:
    """The predicate.

    resolutions: [{state, severity, tags, override}]
    latest_verdict: {sha, result, new_blocking_findings, created_at} or None

    `base_advanced_at` is when a sibling PR last merged onto this PR's base.
    It is the SECOND freshness axis: the head-derived checks below cannot see a
    base move, because merging a sibling leaves this PR's head_sha untouched —
    so without this, N sibling PRs all report converged and merge in sequence,
    each untested against the previous. None (the default, and every row
    predating the field) means "never advanced" and changes nothing.
    """
    reasons: list[str] = []
    counts = ConvergenceCounts()

    # Only an open, non-draft PR targeting the default branch can converge.
    # Merged/closed PRs must never read as "ready to merge", drafts are
    # explicitly not ready, and a stacked PR (base = another feature branch)
    # would silently merge into the wrong branch.
    if pr_state != "open":
        reasons.append(f"pr is {pr_state}")
    if draft:
        reasons.append("pr is a draft")
    if not base_is_default:
        reasons.append("base branch is not the default branch (stacked PR — merge the base first)")

    for r in resolutions:
        state = r.get("state") or "open"
        if state == "deferred":
            counts.deferred += 1
        elif state == "waived":
            counts.waived += 1
        elif resolution_is_blocking(
            state=state,
            severity=r.get("severity") or "medium",
            tags=list(r.get("tags") or []),
            blocking_policy=blocking_policy,
            override=r.get("override") or "",
        ):
            counts.blocking += 1
        else:
            counts.info += 1

    ci_state = compute_ci_state(ci_checks)
    if ci_state != CIState.GREEN:
        reasons.append(f"ci not green (state={ci_state.value})")

    verdict_sha = (latest_verdict or {}).get("sha") or ""
    verdict_result_raw = (latest_verdict or {}).get("result") or ""
    verdict_fresh = bool(latest_verdict) and verdict_sha == head_sha and bool(head_sha)
    verdict_result = VerdictResult(verdict_result_raw) if verdict_result_raw else None

    if latest_verdict is None:
        reasons.append("no verdict recorded")
    elif not verdict_fresh:
        reasons.append(f"verdict is stale (verdict@{verdict_sha[:10]} != head@{head_sha[:10]})")
    elif verdict_result != VerdictResult.APPROVE:
        reasons.append(f"verdict is {verdict_result_raw}, not approve")

    clean_round = bool(
        verdict_fresh
        and verdict_result == VerdictResult.APPROVE
        and int((latest_verdict or {}).get("new_blocking_findings") or 0) == 0
    )
    if require_clean_round and verdict_fresh and verdict_result == VerdictResult.APPROVE and not clean_round:
        reasons.append("last review round raised new blocking findings (clean round required)")

    # Verdict vs BASE. Placed after the head-staleness reasons so the message
    # order reads head-then-base, and only meaningful when a verdict exists —
    # a verdict-less PR already has its own reason above.
    verdict_at = (latest_verdict or {}).get("created_at")
    if base_advanced_at is not None and verdict_at is not None and base_advanced_at > verdict_at:
        reasons.append(
            "base branch advanced since the last review — re-review against the new base"
        )

    if counts.blocking:
        reasons.append(f"{counts.blocking} blocking finding(s) unresolved")

    return ConvergenceState(
        converged=not reasons,
        head_sha=head_sha,
        ci_state=ci_state,
        verdict_fresh=verdict_fresh,
        verdict_result=verdict_result,
        verdict_sha=verdict_sha,
        clean_round=clean_round,
        counts=counts,
        reasons=reasons,
    )


def status_description(state: ConvergenceState) -> str:
    """Human line for the opensweep/converged commit status (§5)."""
    c = state.counts
    if state.converged:
        return f"converged — 0 blocking · {c.deferred} deferred · {c.waived} waived · {c.info} info"
    return (
        f"{c.blocking} blocking · {c.deferred} deferred · {c.waived} waived · {c.info} info — "
        + "; ".join(state.reasons[:2])
    )
