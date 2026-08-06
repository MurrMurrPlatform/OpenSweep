"""Write-gate: "no commits" is a distinct signal from "violation".

Thread-playbook runs finalize on every turn; a turn that produces no commits
(a conversational reply, a plan submission, a Q&A) is expected — it must
audit as quiet, not as a blocked write. `is_only_no_commits` is the guard
callers use to tell "you didn't try to write" from "you tried and it was
denied".
"""

from domains.delivery.services.write_gate import (
    NO_COMMITS_VIOLATION,
    evaluate_changes,
    is_only_no_commits,
)


def test_no_commits_message_matches_the_evaluate_output():
    """The constant MUST equal the string `evaluate_changes` emits — a rename
    on one side without the other silently downgrades every real violation
    to "just a quiet turn"."""
    result = evaluate_changes(
        work_branch="opensweep/x",
        changed_paths=[],
        commits=0,
        denylist=[],
    )
    assert NO_COMMITS_VIOLATION in result.violations


def test_zero_commits_alone_is_a_quiet_turn():
    assert is_only_no_commits([NO_COMMITS_VIOLATION]) is True


def test_a_second_violation_disqualifies_the_quiet_signal():
    """A denylisted path AND no commits is still a real block — the presence
    of any other reason means the caller must audit the turn."""
    violations = [NO_COMMITS_VIOLATION, "path 'auth/keys.py' matches denylisted pattern"]
    assert is_only_no_commits(violations) is False


def test_a_different_message_is_never_the_quiet_signal():
    """Guarding on exact-match matters; a paraphrase must not be miscategorised."""
    assert is_only_no_commits(["no commits on the work branch"]) is False


def test_no_violations_at_all_is_not_the_quiet_signal():
    """The quiet signal is "we tried and produced nothing", not "we didn't
    try" — the latter is a successful gate result."""
    assert is_only_no_commits([]) is False


def test_the_full_write_result_of_a_conversational_turn_matches_the_quiet_signal():
    """End-to-end: a legit opensweep/* branch with no changes and no commits
    produces the single-reason list the guard recognises."""
    result = evaluate_changes(
        work_branch="opensweep/thread-42",
        changed_paths=[],
        commits=0,
        denylist=[],
    )
    assert not result.ok
    assert is_only_no_commits(result.violations)
