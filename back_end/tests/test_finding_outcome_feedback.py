"""Outcome feedback: what a finding turned out to BE feeds its trust score.

The platform stored the human verdict on every triaged finding (`status`) and
ranked on none of it, so nothing in the product ever learned from being wrong:
a repository could dismiss the same false positive ten times and the eleventh
report still ranked exactly as high as the first. `trust_score` now applies
the verdict last, and `trust_score_for` reads it straight off the finding so
the feedback reaches every existing caller (the DTO boundary) with no
plumbing.
"""

from types import SimpleNamespace

import pytest

from domains.findings.services.trust import trust_score, trust_score_for


def _finding(**kwargs):
    base = {
        "confidence": 0.7,
        "source_run_uids": [],
        "source_run_uid": "",
        "detected_by_tool": "",
        "status": "open",
    }
    return SimpleNamespace(**{**base, **kwargs})


def test_an_untriaged_finding_is_unaffected():
    """Nothing changes until a human actually decides something."""
    assert trust_score(confidence=0.7, status="open") == trust_score(confidence=0.7)
    assert trust_score(confidence=0.7, status="") == trust_score(confidence=0.7)


def test_dismissed_sinks_a_finding_the_model_was_sure_about():
    """The case the score exists for: 0.95 model confidence on something a
    human already threw out."""
    assert trust_score(confidence=0.95, status="dismissed") < 0.5


def test_a_human_dismissal_outweighs_every_model_signal():
    fully_corroborated = trust_score(
        confidence=0.9,
        source_run_uids=["r1", "r2", "r3"],
        detected_by_tool="semgrep",
        verification_status="confirmed",
    )
    same_but_dismissed = trust_score(
        confidence=0.9,
        source_run_uids=["r1", "r2", "r3"],
        detected_by_tool="semgrep",
        verification_status="confirmed",
        status="dismissed",
    )
    assert same_but_dismissed < fully_corroborated
    # Three agreeing runs and a static analyzer do not rescue it.
    assert same_but_dismissed < trust_score(confidence=0.5)


def test_being_fixed_is_proof_the_finding_was_real():
    assert trust_score(confidence=0.5, status="fixed") > trust_score(confidence=0.5)


@pytest.mark.parametrize("status", ["wont-fix", "accepted", "acknowledged"])
def test_living_with_an_issue_is_not_calling_it_wrong(status):
    """`wont-fix` and friends mean "real, but we accept it" — only
    `dismissed` means the finding was not a real problem."""
    assert trust_score(confidence=0.6, status=status) >= trust_score(confidence=0.6)


def test_superseded_demotes_without_calling_the_finding_wrong():
    superseded = trust_score(confidence=0.6, status="superseded")
    dismissed = trust_score(confidence=0.6, status="dismissed")
    assert dismissed < superseded < trust_score(confidence=0.6)


def test_outcome_stays_within_bounds():
    assert trust_score(confidence=1.0, status="fixed") <= 1.0
    assert trust_score(confidence=0.0, status="dismissed") >= 0.0


def test_status_is_read_off_the_finding_so_every_caller_gets_the_feedback():
    """`finding_to_dto` calls `trust_score_for(f)` with no extra arguments —
    if the verdict were a parameter instead, nothing would ever pass it."""
    assert trust_score_for(_finding(status="dismissed")) < trust_score_for(_finding())
    assert trust_score_for(_finding(status="fixed")) > trust_score_for(_finding())


def test_unknown_or_missing_status_is_ignored_not_fatal():
    assert trust_score_for(_finding(status=None)) == trust_score_for(_finding(status="open"))
    assert trust_score_for(SimpleNamespace()) == trust_score(confidence=0.7)
    assert trust_score(confidence=0.7, status="TOTALLY-MADE-UP") == trust_score(confidence=0.7)


def test_status_matching_ignores_case():
    assert trust_score(confidence=0.6, status="Dismissed") == trust_score(
        confidence=0.6, status="dismissed"
    )
