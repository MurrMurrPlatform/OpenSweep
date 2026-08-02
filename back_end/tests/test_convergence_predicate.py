"""Convergence predicate — the definition of done (PLATFORM_V2_DESIGN.md §5).

Pure tests, no Neo4j. Each case encodes a failure mode observed in the
GitHub-Actions-era workflow on MurrMurr PRs #69/#72.
"""

from domains.delivery.schemas import CIState, VerdictResult
from domains.delivery.services.convergence import (
    compute_ci_state,
    compute_convergence,
    resolution_is_blocking,
    severity_blocks,
    status_description,
)

HEAD = "a" * 40
OLD = "b" * 40

POLICY = {"default": "high", "per_tag": {"security": "medium"}}


def _check(name="ci", status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


def _approve(sha=HEAD, new_blocking=0):
    return {"sha": sha, "result": "approve", "new_blocking_findings": new_blocking}


# ── CI rollup strictness (§5.1) ─────────────────────────────────────────────


def test_empty_rollup_is_not_green():
    # The auto-merge-and-close hole: `not any(failures)` on [] read as passing.
    assert compute_ci_state([]) == CIState.EMPTY


def test_pending_rollup_is_not_green():
    assert compute_ci_state([_check(), _check(status="in_progress", conclusion=None)]) == CIState.PENDING


def test_any_failure_is_red():
    assert compute_ci_state([_check(), _check(conclusion="failure")]) == CIState.RED
    assert compute_ci_state([_check(conclusion="timed_out")]) == CIState.RED
    assert compute_ci_state([_check(conclusion="cancelled")]) == CIState.RED


def test_all_success_is_green():
    assert compute_ci_state([_check(), _check(conclusion="skipped"), _check(conclusion="neutral")]) == CIState.GREEN


def test_unknown_conclusion_never_green():
    assert compute_ci_state([_check(conclusion="mystery")]) == CIState.PENDING


# ── Blocking policy (§4) ────────────────────────────────────────────────────


def test_severity_thresholds():
    assert severity_blocks("critical", ["correctness"], POLICY)
    assert severity_blocks("high", ["correctness"], POLICY)
    assert not severity_blocks("medium", ["correctness"], POLICY)
    # security tightens to medium
    assert severity_blocks("medium", ["security"], POLICY)
    assert not severity_blocks("low", ["security"], POLICY)


def test_terminal_and_parked_states_never_block():
    # refuted = machine-disproved by a verification run (§A)
    for state in ("verified", "deferred", "waived", "refuted"):
        assert not resolution_is_blocking(
            state=state, severity="critical", tags=["security"], blocking_policy=POLICY
        )


def test_fixed_but_unverified_still_blocks():
    # "fixed is claimed, verified is granted"
    assert resolution_is_blocking(state="fixed", severity="high", tags=["correctness"], blocking_policy=POLICY)


def test_human_override_wins_both_ways():
    assert not resolution_is_blocking(
        state="open", severity="critical", tags=["security"], blocking_policy=POLICY, override="allow"
    )
    assert resolution_is_blocking(
        state="open", severity="low", tags=["docs"], blocking_policy=POLICY, override="block"
    )


# ── The predicate (§5) ──────────────────────────────────────────────────────


def _converge(**kw):
    defaults = dict(
        head_sha=HEAD,
        ci_checks=[_check()],
        latest_verdict=_approve(),
        resolutions=[],
        blocking_policy=POLICY,
        require_clean_round=True,
    )
    defaults.update(kw)
    return compute_convergence(**defaults)


def test_happy_path_converges():
    state = _converge()
    assert state.converged
    assert state.reasons == []
    assert state.clean_round
    assert state.verdict_fresh


def test_no_verdict_blocks():
    state = _converge(latest_verdict=None)
    assert not state.converged
    assert any("no verdict" in r for r in state.reasons)


def test_stale_verdict_blocks():
    # PR #69: push after approval must invalidate the verdict.
    state = _converge(latest_verdict=_approve(sha=OLD))
    assert not state.converged
    assert not state.verdict_fresh
    assert any("stale" in r for r in state.reasons)


def test_needs_human_blocks():
    state = _converge(latest_verdict={"sha": HEAD, "result": "needs_human", "new_blocking_findings": 0})
    assert not state.converged
    assert state.verdict_result == VerdictResult.NEEDS_HUMAN


def test_dirty_round_blocks_when_required():
    # Approve that still raised new blocking findings is not a clean round.
    state = _converge(latest_verdict=_approve(new_blocking=2))
    assert not state.converged
    assert not state.clean_round
    assert any("clean round" in r for r in state.reasons)


def test_dirty_round_allowed_when_policy_relaxed():
    state = _converge(latest_verdict=_approve(new_blocking=2), require_clean_round=False)
    assert state.converged


def test_blocking_resolution_blocks():
    state = _converge(resolutions=[{"state": "open", "severity": "high", "tags": ["correctness"]}])
    assert not state.converged
    assert state.counts.blocking == 1


def test_deferred_and_waived_never_block_but_are_counted():
    # PR #72 rebuttals: parked findings stay visible, never hidden.
    state = _converge(
        resolutions=[
            {"state": "deferred", "severity": "critical", "tags": ["security"]},
            {"state": "waived", "severity": "high", "tags": ["correctness"]},
            {"state": "open", "severity": "low", "tags": ["docs"]},
        ]
    )
    assert state.converged
    assert state.counts.deferred == 1
    assert state.counts.waived == 1
    assert state.counts.info == 1


def test_empty_ci_blocks_even_with_fresh_approve():
    # PR #72: stacked PR with zero CI must not read as done.
    state = _converge(ci_checks=[])
    assert not state.converged
    assert state.ci_state == CIState.EMPTY


# ── PR state / draft / stacked-base gates ───────────────────────────────────


def test_merged_pr_never_converges():
    state = _converge(pr_state="merged")
    assert not state.converged
    assert "pr is merged" in state.reasons


def test_closed_pr_never_converges():
    state = _converge(pr_state="closed")
    assert not state.converged
    assert "pr is closed" in state.reasons


def test_draft_pr_never_converges():
    state = _converge(draft=True)
    assert not state.converged
    assert "pr is a draft" in state.reasons


def test_stacked_pr_never_converges():
    # Base is another feature branch — merging would land on the wrong branch.
    state = _converge(base_is_default=False)
    assert not state.converged
    assert any("stacked" in r for r in state.reasons)
    assert any("base branch is not the default branch" in r for r in state.reasons)


def test_open_nondraft_default_base_still_converges():
    state = _converge(pr_state="open", draft=False, base_is_default=True)
    assert state.converged


def test_status_description_shape():
    good = _converge()
    assert status_description(good).startswith("converged — 0 blocking")
    bad = _converge(resolutions=[{"state": "open", "severity": "high", "tags": ["correctness"]}])
    assert "1 blocking" in status_description(bad)


# ── Second freshness axis: verdict vs BASE ──────────────────────────────────
# Merging a sibling does not move THIS PR's head_sha, so every head-derived
# check above still reads "fresh". Without this axis N siblings all report
# converged and merge in sequence, each untested against the previous.

from datetime import UTC, datetime  # noqa: E402


def _converging_kwargs(**over):
    """A PR that converges on every existing axis, so a failure below can only
    come from the base-advanced check."""
    base = dict(
        head_sha="a" * 40,
        ci_checks=[{"name": "ci", "status": "completed", "conclusion": "success"}],
        latest_verdict={
            "sha": "a" * 40,
            "result": "approve",
            "new_blocking_findings": 0,
            "created_at": datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        },
        resolutions=[],
        blocking_policy={},
    )
    base.update(over)
    return base


def test_baseline_converges_without_the_new_axis():
    assert compute_convergence(**_converging_kwargs()).converged


def test_none_base_advanced_at_changes_nothing():
    """Every row predating the field reads None. It must be inert."""
    assert compute_convergence(**_converging_kwargs(base_advanced_at=None)).converged


def test_base_advancing_after_the_verdict_blocks():
    state = compute_convergence(
        **_converging_kwargs(base_advanced_at=datetime(2026, 8, 2, 13, 0, tzinfo=UTC))
    )
    assert not state.converged
    assert any("base branch advanced" in r for r in state.reasons)


def test_base_advancing_BEFORE_the_verdict_is_fine():
    """The review already saw the new base — re-blocking would wedge the PR
    forever, since nothing re-reviews a PR whose head didn't move."""
    assert compute_convergence(
        **_converging_kwargs(base_advanced_at=datetime(2026, 8, 2, 11, 0, tzinfo=UTC))
    ).converged


def test_a_verdictless_pr_does_not_gain_a_second_reason():
    """It already has 'no verdict recorded'; adding a base reason would be
    noise, and there is no created_at to compare against."""
    state = compute_convergence(
        **_converging_kwargs(
            latest_verdict=None,
            base_advanced_at=datetime(2026, 8, 2, 13, 0, tzinfo=UTC),
        )
    )
    assert not any("base branch advanced" in r for r in state.reasons)
