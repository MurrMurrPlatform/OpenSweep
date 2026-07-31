"""Group flow (Phase 5): one thread on the parent → one PR covers the epic."""

from types import SimpleNamespace

from domains.threads.services.intents import (
    build_epic_addendum,
    build_epic_review_checklist,
)


def _child(uid, title, desc="", ac=None):
    return SimpleNamespace(
        uid=uid, title=title, description=desc, acceptance_criteria=ac or []
    )


def test_no_children_no_addendum():
    assert build_epic_addendum([]) == ""


def test_children_are_listed_with_acceptance():
    out = build_epic_addendum(
        [
            _child("c-1", "Fix login", "500 on login", ["login works"]),
            _child("c-2", "Fix logout"),
        ]
    )
    assert "ALL subtickets" in out
    assert "c-1" in out and "Fix login" in out and "login works" in out
    assert "c-2" in out and "(no description)" in out


def test_long_descriptions_truncate():
    out = build_epic_addendum([_child("c-1", "T", "x" * 1000)])
    assert "…" in out and len(out) < 900


# ── Review checklist (epic coverage gate) ────────────────────────────────────


def test_review_checklist_empty_without_children():
    assert build_epic_review_checklist([]) == ""


def test_review_checklist_lists_members_and_demands_blocking_findings():
    out = build_epic_review_checklist(
        [
            _child("c-1", "Fix login", "500 on login", ["login works"]),
            _child("c-2", "Fix logout"),
        ]
    )
    assert "Epic subticket coverage" in out
    assert "c-1" in out and "Fix login" in out and "login works" in out
    assert "c-2" in out and "(no description)" in out
    # An unmet subticket must block the merge, not merely be mentioned.
    assert "new_blocking_findings" in out
    assert "met, unmet, or not attempted" in out
