"""Delivery domain — PR convergence ledger (PLATFORM_V2_DESIGN.md §3–§5).

PullRequest mirrors a GitHub PR (synced by webhook, head-driven recompute).
Verdict is one review's judgment bound to an exact commit; any push invalidates.
FindingResolution is the per-PR lifecycle of a Finding — the Finding itself
stays repository-scoped (v1-compatible); its fixed/verified/waived/deferred
state against a specific PR lives here.
MergePolicy declares, per repository, which finding severities block merge.
WebhookDelivery records processed GitHub delivery ids so replays are no-ops.
"""

from neomodel import (
    AsyncStructuredNode,
    BooleanProperty,
    DateTimeProperty,
    IntegerProperty,
    JSONProperty,
    StringProperty,
)


def pr_key(repository_uid: str, github_number: int) -> str:
    return f"{repository_uid}:{github_number}"


def resolution_key(finding_uid: str, pull_request_uid: str) -> str:
    return f"{finding_uid}:{pull_request_uid}"


class PullRequest(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    repository_uid = StringProperty(required=True, index=True)
    github_number = IntegerProperty(required=True)
    pr_key = StringProperty(unique_index=True, required=True)  # "{repository_uid}:{number}"

    title = StringProperty(default="")
    author = StringProperty(default="")
    url = StringProperty(default="")
    state = StringProperty(default="open", index=True)  # open | closed | merged
    draft = BooleanProperty(default=False)

    head_sha = StringProperty(default="", index=True)
    head_ref = StringProperty(default="")
    base_ref = StringProperty(default="")
    base_is_default = BooleanProperty(default=True)
    # When a SIBLING PR on this same base last merged. Convergence is
    # head-derived, so a sibling merging does not move this PR's head_sha —
    # its verdict stays "fresh" and its published converged status stays green
    # against a base that has moved. This is the second freshness axis:
    # verdict vs BASE, not just verdict vs HEAD.
    #
    # Deliberately NOT backfilled: None reads as "never advanced", which is the
    # correct, non-disruptive default for every existing row.
    base_advanced_at = DateTimeProperty()

    ticket_uid = StringProperty(default="", index=True)

    # Strict CI rollup at head_sha — empty/pending are NOT green (§5.1).
    ci_state = StringProperty(default="empty")  # green | red | pending | empty
    ci_checks = JSONProperty(default=[])  # [{name, status, conclusion, url}]

    # Bounded auto-fix loop counter (§6); replaces comment-marker counting.
    fix_rounds = IntegerProperty(default=0)
    # Denormalized `fix_rounds >= MergePolicy.max_fix_rounds`, refreshed on
    # every recompute + at fix-run dispatch/reset so DTO consumers don't need
    # the policy in hand.
    fix_rounds_exhausted = BooleanProperty(default=False)

    converged = BooleanProperty(default=False, index=True)
    convergence = JSONProperty(default={})  # last ConvergenceState snapshot

    created_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)
    last_synced_at = DateTimeProperty()


class Verdict(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    pull_request_uid = StringProperty(required=True, index=True)
    repository_uid = StringProperty(required=True, index=True)
    sha = StringProperty(required=True, index=True)

    result = StringProperty(required=True)  # approve | request_changes | needs_human
    # Blocking findings newly raised by the review that produced this verdict —
    # the clean-round input (§5.3).
    new_blocking_findings = IntegerProperty(default=0)
    finding_uids = JSONProperty(default=[])
    # [{criterion, result: pass|fail|unverifiable, note}]
    ac_results = JSONProperty(default=[])
    # [{assumption, result: confirmed|refuted|unverifiable, note}] — the
    # reviewer's judgment on assumptions the implementer made instead of
    # asking. A SIBLING of ac_results, never merged into it: "did the PR meet
    # the criterion" and "was the guess right" are different questions, and
    # folding them together would corrupt the AC pass-rate.
    assumption_results = JSONProperty(default=[])

    source_run_uid = StringProperty(default="", index=True)
    executor = StringProperty(default="manual")

    # Skeptic pass (per-repo stage_auto("verify")). "" = no verification;
    # pending → a verification run will judge this verdict's findings;
    # adjusted → this verdict was produced BY verification (supersedes);
    # superseded → an adjusted verdict at the same sha replaced this one;
    # failed → the verification run never completed (verdict stays operative).
    verification_status = StringProperty(default="")
    verification_run_uid = StringProperty(default="")  # dispatch idempotence

    created_at = DateTimeProperty(default_now=True)


class FindingVerification(AsyncStructuredNode):
    """One verification judgment on one finding, produced by a verify run.

    The run's job is to REFUTE: `refuted` requires affirmative evidence the
    claimed failure cannot occur at the pinned sha. Findings the run never
    reports on are treated as confirmed (fail closed for merge safety)."""

    uid = StringProperty(unique_index=True, required=True)
    pull_request_uid = StringProperty(required=True, index=True)
    repository_uid = StringProperty(required=True, index=True)
    verdict_uid = StringProperty(required=True, index=True)
    finding_uid = StringProperty(required=True, index=True)
    run_uid = StringProperty(required=True, index=True)
    # One judgment per finding per run.
    verification_key = StringProperty(unique_index=True, required=True)  # "{run_uid}:{finding_uid}"
    sha = StringProperty(default="")

    result = StringProperty(required=True)  # confirmed | refuted | needs-human
    reasoning = StringProperty(default="")

    created_at = DateTimeProperty(default_now=True)


def verification_key(run_uid: str, finding_uid: str) -> str:
    return f"{run_uid}:{finding_uid}"


class FindingResolution(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    finding_uid = StringProperty(required=True, index=True)
    pull_request_uid = StringProperty(required=True, index=True)
    repository_uid = StringProperty(required=True, index=True)
    resolution_key = StringProperty(unique_index=True, required=True)  # "{finding_uid}:{pr_uid}"

    introduced_at_sha = StringProperty(default="")

    state = StringProperty(default="open", index=True)
    # open | in-fix | fixed | verified | reopened | deferred | waived

    fixed_at_sha = StringProperty(default="")
    verified_at_sha = StringProperty(default="")
    verified_by_run_uid = StringProperty(default="")

    waived_by = StringProperty(default="")
    waive_reason = StringProperty(default="")

    # Agents may REQUEST a waiver (lands in the Needs-You queue); only a human
    # actually waives (§11 role gating). No state change until a human acts.
    waive_requested_by = StringProperty(default="")
    waive_requested_reason = StringProperty(default="")

    # "" (policy decides) | "block" | "allow" — human override, reason required.
    blocking_override = StringProperty(default="")
    blocking_override_reason = StringProperty(default="")

    ticket_uid = StringProperty(default="")  # set when deferred

    created_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)


# Write-path path denylist defaults (§6) — regexes matched against every
# changed path in a write sandbox before the platform pushes. Port of the
# docs/safety.md concept: auth, payments, migrations, secrets, deployment.
DEFAULT_PATH_DENYLIST = [
    "(^|/)auth/",
    "(^|/)payments?/",
    "(^|/)migrations?/",
    "\\.env",
    "secrets?",
    "(^|/)deployment/",
]


class MergePolicy(AsyncStructuredNode):
    uid = StringProperty(unique_index=True, required=True)
    repository_uid = StringProperty(unique_index=True, required=True)

    # {"default": "high", "per_tag": {"security": "medium"}} — severities at
    # or above the threshold block merge; per-tag thresholds match against
    # the finding's free-text tags (strictest match wins).
    blocking = JSONProperty(default={"default": "high", "per_tag": {"security": "medium"}})
    require_clean_round = BooleanProperty(default=True)
    max_fix_rounds = IntegerProperty(default=2)

    # How the in-app merge button merges a converged PR: squash | merge | rebase.
    # Squash by default — the delivery loop produces one logical change per PR.
    merge_method = StringProperty(default="squash")

    # Write-path denylist (list of regex strings). None on pre-Phase-3 nodes
    # means "use DEFAULT_PATH_DENYLIST"; an explicit [] is an operator opt-out.
    path_denylist = JSONProperty(default=list(DEFAULT_PATH_DENYLIST))

    created_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)


class WebhookDelivery(AsyncStructuredNode):
    """Processed GitHub webhook delivery ids — replays become no-ops.

    `status` tracks the processing outcome so a delivery that FAILED is not
    permanently dropped: only `succeeded` deliveries are duplicates; failed
    (or stale in-flight) ones are reprocessed on GitHub redelivery. Head-driven
    sync makes reprocessing idempotent."""

    delivery_id = StringProperty(unique_index=True, required=True)
    event = StringProperty(default="")
    action = StringProperty(default="")
    # processing | succeeded | failed
    status = StringProperty(default="processing", index=True)
    attempts = IntegerProperty(default=0)
    received_at = DateTimeProperty(default_now=True)
    updated_at = DateTimeProperty(default_now=True)


WEBHOOK_DELIVERY_STATUSES = {"processing", "succeeded", "failed"}


PR_STATES = {"open", "closed", "merged"}

CI_STATES = {"green", "red", "pending", "empty"}

VERDICT_RESULTS = {"approve", "request_changes", "needs_human"}

VERIFICATION_RESULTS = {"confirmed", "refuted", "needs-human"}

VERDICT_VERIFICATION_STATUSES = {"", "pending", "adjusted", "superseded", "failed"}

RESOLUTION_STATES = {
    "open", "in-fix", "fixed", "verified", "reopened", "deferred", "waived", "refuted",
}

# States that can hold up a merge (fixed-but-unverified still blocks — §4:
# "fixed is claimed, verified is granted"). Deferred and waived never block;
# refuted (machine-disproved by a verification run) never blocks either.
BLOCKING_CAPABLE_STATES = {"open", "in-fix", "fixed", "reopened"}

NON_BLOCKING_STATES = {"verified", "deferred", "waived", "refuted"}

AC_RESULTS = {"pass", "fail", "unverifiable"}
