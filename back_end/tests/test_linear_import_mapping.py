"""Linear → OpenSweep mapping functions (scripts/linear_import.py) — pure, no network."""

from scripts.linear_import import (
    build_description,
    is_already_imported,
    issue_to_ticket_fields,
    map_labels,
    map_priority,
    map_state_type,
)

# ── Priority mapping ─────────────────────────────────────────────────────────


def test_map_priority_from_linear_numbers():
    # Linear: 0=none, 1=urgent, 2=high, 3=medium, 4=low
    assert map_priority(0) == "medium"
    assert map_priority(1) == "urgent"
    assert map_priority(2) == "high"
    assert map_priority(3) == "medium"
    assert map_priority(4) == "low"


def test_map_priority_from_labels():
    assert map_priority("Urgent") == "urgent"
    assert map_priority("high") == "high"
    assert map_priority("Medium") == "medium"
    assert map_priority("low") == "low"
    assert map_priority("No priority") == "medium"
    assert map_priority("none") == "medium"


def test_map_priority_garbage_defaults_to_medium():
    assert map_priority(None) == "medium"
    assert map_priority(99) == "medium"
    assert map_priority("someday") == "medium"
    assert map_priority(True) == "medium"


# ── State mapping ────────────────────────────────────────────────────────────


def test_map_state_type_open_states():
    assert map_state_type("backlog") == "backlog"
    assert map_state_type("unstarted") == "backlog"
    assert map_state_type("started") == "todo"


def test_map_state_type_canceled_becomes_done():
    assert map_state_type("canceled") == "done"


def test_map_state_type_skips_completed_triage_unknown():
    assert map_state_type("completed") is None
    assert map_state_type("triage") is None
    assert map_state_type("") is None
    assert map_state_type("weird") is None


# ── Labels ───────────────────────────────────────────────────────────────────


def test_map_labels_passthrough_and_dedupe():
    assert map_labels(["bug", "backend", "bug"], "started") == ["bug", "backend"]


def test_map_labels_canceled_gains_wont_fix():
    assert map_labels(["bug"], "canceled") == ["bug", "wont-fix"]
    assert map_labels(["wont-fix"], "canceled") == ["wont-fix"]  # no duplicate
    assert map_labels([], "canceled") == ["wont-fix"]


# ── Description provenance + idempotency ─────────────────────────────────────


def test_build_description_appends_provenance_footer():
    d = build_description("Fix the thing.", "MUR-42", "https://linear.app/x/issue/MUR-42")
    assert d.startswith("Fix the thing.")
    assert "MUR-42" in d
    assert "https://linear.app/x/issue/MUR-42" in d


def test_build_description_empty_body_is_just_the_footer():
    d = build_description("", "MUR-7", "")
    assert "MUR-7" in d
    assert not d.startswith("\n")


def test_is_already_imported_matches_identifier_in_descriptions():
    existing = ["something", build_description("body", "MUR-42", "u")]
    assert is_already_imported("MUR-42", existing)
    assert not is_already_imported("MUR-43", existing)
    assert not is_already_imported("MUR-1", [None, ""])  # type: ignore[list-item]


def test_is_already_imported_does_not_confuse_prefix_identifiers():
    # MUR-1 must not match a description that only mentions MUR-11 / MUR-100 —
    # bare substring match would silently under-import every prefix identifier.
    existing = [
        build_description("body", "MUR-11", "https://linear.app/x/issue/MUR-11"),
        build_description("body", "MUR-100", "https://linear.app/x/issue/MUR-100"),
    ]
    assert not is_already_imported("MUR-1", existing)
    assert not is_already_imported("MUR-10", existing)
    assert is_already_imported("MUR-11", existing)
    assert is_already_imported("MUR-100", existing)


def test_is_already_imported_empty_identifier_is_false():
    existing = [build_description("body", "MUR-1", "u")]
    assert not is_already_imported("", existing)


# ── Whole-issue mapping ──────────────────────────────────────────────────────


def _issue(state_type: str, **overrides) -> dict:
    issue = {
        "identifier": "MUR-10",
        "title": "Do the work",
        "description": "Details here.",
        "url": "https://linear.app/example/issue/MUR-10",
        "priority": 2,
        "state": {"name": "In Progress", "type": state_type},
        "labels": {"nodes": [{"name": "backend"}]},
    }
    issue.update(overrides)
    return issue


def test_issue_to_ticket_fields_started_maps_to_todo():
    fields = issue_to_ticket_fields(_issue("started"), "repo-1")
    assert fields is not None
    assert fields["status"] == "todo"
    assert fields["repository_uid"] == "repo-1"
    assert fields["title"] == "Do the work"
    assert fields["priority"] == "high"
    assert fields["origin"] == "human"
    assert fields["labels"] == ["backend"]
    assert "MUR-10" in fields["description"]


def test_issue_to_ticket_fields_backlog_and_unstarted_map_to_backlog():
    for state in ("backlog", "unstarted"):
        fields = issue_to_ticket_fields(_issue(state), "repo-1")
        assert fields is not None and fields["status"] == "backlog"


def test_issue_to_ticket_fields_canceled_is_done_wont_fix():
    fields = issue_to_ticket_fields(_issue("canceled"), "repo-1")
    assert fields is not None
    assert fields["status"] == "done"
    assert "wont-fix" in fields["labels"]


def test_issue_to_ticket_fields_completed_is_skipped():
    assert issue_to_ticket_fields(_issue("completed"), "repo-1") is None


def test_issue_to_ticket_fields_tolerates_missing_optionals():
    issue = {"identifier": "MUR-3", "state": {"type": "backlog"}}
    fields = issue_to_ticket_fields(issue, "repo-1")
    assert fields is not None
    assert fields["title"] == "MUR-3"  # falls back to identifier
    assert fields["priority"] == "medium"
    assert fields["labels"] == []
    assert "MUR-3" in fields["description"]
