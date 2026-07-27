"""A verdict's blocking count is derived from policy, not self-reported.

`new_blocking_findings` gates the clean-round condition (convergence §5.3) and
therefore the published `opensweep/converged` status. The review intent asks
the agent to "count how many of YOUR new findings are blocking under this
policy", so trusting the submitted number let the model being graded mark its
own paper: answer 0 and a clean round — and a green status — followed.
"""

import pytest

from domains.delivery.schemas import VerdictResult
from domains.delivery.services.convergence import (
    compute_convergence,
    count_blocking_findings,
)

POLICY = {"default": "high", "per_tag": {"security": "medium"}}


def _f(severity, tags=(), override=""):
    return {"severity": severity, "tags": list(tags), "override": override}


def test_counts_by_default_threshold():
    findings = [_f("critical"), _f("high"), _f("medium"), _f("low")]
    assert count_blocking_findings(findings, POLICY) == 2


def test_per_tag_threshold_is_stricter():
    """`security` blocks from medium up, so a medium security finding counts."""
    assert count_blocking_findings([_f("medium", ["security"])], POLICY) == 1
    assert count_blocking_findings([_f("medium", ["style"])], POLICY) == 0


def test_strictest_matching_tag_wins():
    assert count_blocking_findings([_f("medium", ["style", "security"])], POLICY) == 1


def test_human_override_beats_policy_both_ways():
    assert count_blocking_findings([_f("critical", override="allow")], POLICY) == 0
    assert count_blocking_findings([_f("low", override="block")], POLICY) == 1


def test_no_findings_is_zero():
    assert count_blocking_findings([], POLICY) == 0


@pytest.mark.parametrize("reported", [0, 99])
def test_convergence_ignores_what_the_model_claimed(reported):
    """The headline scenario: a review that raised a blocking finding but
    reported 0 must NOT produce a clean round.

    `compute_convergence` reads the stored count, and `submit_verdict` now
    stores the DERIVED one — so the value reaching this predicate no longer
    depends on `reported` at all.
    """
    derived = count_blocking_findings([_f("critical")], POLICY)
    assert derived == 1

    state = compute_convergence(
        head_sha="abc123",
        ci_checks=[{"status": "completed", "conclusion": "success"}],
        latest_verdict={
            "sha": "abc123",
            "result": VerdictResult.APPROVE,
            # What submit_verdict persists — derived, regardless of `reported`.
            "new_blocking_findings": derived,
        },
        resolutions=[],
        blocking_policy=POLICY,
        require_clean_round=True,
    )

    assert not state.converged
    assert any("clean round" in r for r in state.reasons)


def test_clean_round_still_converges_when_nothing_blocks():
    """The fix must not make convergence unreachable."""
    derived = count_blocking_findings([_f("low")], POLICY)
    assert derived == 0

    state = compute_convergence(
        head_sha="abc123",
        ci_checks=[{"status": "completed", "conclusion": "success"}],
        latest_verdict={
            "sha": "abc123",
            "result": VerdictResult.APPROVE,
            "new_blocking_findings": derived,
        },
        resolutions=[],
        blocking_policy=POLICY,
        require_clean_round=True,
    )

    assert state.converged, state.reasons
