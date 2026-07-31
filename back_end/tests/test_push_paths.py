"""Reading a GitHub push payload for freshness — the pure decision.

This is where the two historical freshness bugs lived, so they get named
tests: a push to ANY branch used to mark areas stale, and changed paths were
read from `payload["commits"]`, which GitHub caps at 20 commits.
"""

from domains.repositories.services.push_paths import (
    NULL_SHA,
    PAYLOAD_COMMIT_CAP,
    changed_paths_from_push,
)


def _push(**kw):
    payload = {
        "ref": "refs/heads/main",
        "before": "a" * 40,
        "after": "b" * 40,
        "commits": [{"added": ["x.py"], "modified": [], "removed": []}],
    }
    payload.update(kw)
    return payload


def test_default_branch_push_is_accepted():
    result = changed_paths_from_push(_push(), "main")
    assert result.accepted
    assert result.branch == "main"
    assert result.payload_paths == ["x.py"]
    assert result.comparable


def test_non_default_branch_is_rejected():
    """The headline fix: a feature-branch push is not a change to the codebase."""
    result = changed_paths_from_push(_push(ref="refs/heads/feat"), "main")
    assert not result.accepted
    assert "not the default branch" in result.reason
    assert not result.comparable


def test_default_branch_is_read_from_the_repository_not_hardcoded():
    result = changed_paths_from_push(_push(ref="refs/heads/trunk"), "trunk")
    assert result.accepted
    assert changed_paths_from_push(_push(ref="refs/heads/main"), "trunk").accepted is False


def test_tags_are_not_branches():
    result = changed_paths_from_push(_push(ref="refs/tags/v1.0.0"), "main")
    assert not result.accepted
    assert "not a branch" in result.reason


def test_branch_deletion_marks_nothing():
    result = changed_paths_from_push(_push(deleted=True), "main")
    assert not result.accepted
    assert result.deleted
    assert not result.comparable


def test_branch_creation_has_no_comparable_range():
    """A null `before` means there is no before..after to compare — the commit
    array is all we have."""
    result = changed_paths_from_push(_push(before=NULL_SHA, created=True), "main")
    assert result.accepted
    assert result.created
    assert not result.comparable
    assert result.payload_paths == ["x.py"]


def test_commit_array_truncation_is_flagged():
    """GitHub caps `commits` at 20; at the cap the path list is definitely
    incomplete and the caller must degrade loudly rather than assume it saw
    everything."""
    commits = [
        {"added": [f"f{i}.py"], "modified": [], "removed": []}
        for i in range(PAYLOAD_COMMIT_CAP)
    ]
    result = changed_paths_from_push(_push(commits=commits), "main")
    assert result.commits_truncated
    assert len(result.payload_paths) == PAYLOAD_COMMIT_CAP

    small = changed_paths_from_push(_push(commits=commits[:3]), "main")
    assert not small.commits_truncated


def test_added_modified_and_removed_all_count():
    result = changed_paths_from_push(
        _push(commits=[{"added": ["a"], "modified": ["b"], "removed": ["c"]}]), "main"
    )
    assert set(result.payload_paths) == {"a", "b", "c"}


def test_paths_are_deduped_across_commits():
    result = changed_paths_from_push(
        _push(
            commits=[
                {"added": [], "modified": ["same.py"], "removed": []},
                {"added": [], "modified": ["same.py"], "removed": []},
            ]
        ),
        "main",
    )
    assert result.payload_paths == ["same.py"]


def test_malformed_payloads_are_rejected_not_raised():
    """The webhook must still return 200 — a bad payload is a rejection."""
    assert not changed_paths_from_push({}, "main").accepted
    assert not changed_paths_from_push([], "main").accepted  # type: ignore[arg-type]
    assert not changed_paths_from_push({"ref": "refs/heads/"}, "main").accepted
    weird = changed_paths_from_push(
        {"ref": "refs/heads/main", "commits": ["not-a-dict", None]}, "main"
    )
    assert weird.accepted and weird.payload_paths == []


def test_empty_default_branch_falls_back_to_main():
    assert changed_paths_from_push(_push(), "").accepted
