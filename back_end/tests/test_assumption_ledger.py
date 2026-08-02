"""The assumption ledger: dedup, rendering, and the review handoff.

Under `autonomy=assume` an unasked question must become a visible artifact.
These cover the parts that decide whether it stays visible: idempotent
recording, a PR body that is unchanged when the ledger is empty, and a review
block that reaches the reviewer for non-epic tickets too.
"""

from types import SimpleNamespace

from domains.platform_tools.assumptions import fingerprint, merge_assumption
from domains.threads.services.intents import (
    build_assumption_review_block,
    render_assumptions_md,
)


def _a(text, because="", confidence="medium", result="unreviewed", note=""):
    return {
        "assumption": text,
        "because": because,
        "confidence": confidence,
        "result": result,
        "note": note,
        "question": "",
        "source_run_uid": "",
        "ts": "2026-08-02T12:00:00+00:00",
    }


def test_fingerprint_ignores_case_and_whitespace():
    assert fingerprint("  Use   the Existing helper ") == fingerprint(
        "use the existing helper"
    )


def test_a_new_assumption_appends():
    out = merge_assumption([_a("A")], _a("B"))
    assert [r["assumption"] for r in out] == ["A", "B"]


def test_restating_an_assumption_replaces_rather_than_duplicates():
    """A resumed or continued run re-reads its own context and re-states what
    it assumed. Without dedup the ledger grows a copy every pass."""
    out = merge_assumption([_a("Use the existing helper", because="old")],
                           _a("use  the existing HELPER", because="new"))
    assert len(out) == 1
    assert out[0]["because"] == "new"


def test_a_reviewers_verdict_survives_the_agent_restating_it():
    """The important one: a human (or the review agent) marked this refuted and
    acted on it. The implementer rewording its rationale must not silently
    reset that to unreviewed."""
    existing = [_a("X", result="refuted", note="the code does the opposite")]
    out = merge_assumption(existing, _a("x", because="reworded"))
    assert out[0]["result"] == "refuted"
    assert out[0]["note"] == "the code does the opposite"
    assert out[0]["because"] == "reworded"


def test_an_unreviewed_prior_does_not_pin_the_update():
    out = merge_assumption([_a("X", result="unreviewed")], _a("X", because="why"))
    assert out[0]["result"] == "unreviewed"
    assert out[0]["because"] == "why"


def test_empty_ledger_renders_nothing_so_pr_bodies_are_unchanged():
    """An `interrogate` run must produce a byte-identical PR body to before the
    dial existed."""
    assert render_assumptions_md([]) == ""
    assert render_assumptions_md([{"assumption": "   "}]) == ""


def test_rendered_block_carries_the_rationale_and_confidence():
    md = render_assumptions_md([_a("Retry twice", because="matches http_client.py:40")])
    assert "Retry twice" in md
    assert "http_client.py:40" in md
    assert "confidence: medium" in md


def test_review_block_is_empty_when_nothing_was_assumed():
    tickets = [SimpleNamespace(uid="t1", assumptions=[])]
    assert build_assumption_review_block(tickets) == ""


def test_review_block_covers_a_single_non_epic_ticket():
    """The non-epic case is the common one — most tickets have no children, and
    an assumption there must still be adjudicated."""
    tickets = [SimpleNamespace(uid="t1", assumptions=[_a("Assumed X")])]
    block = build_assumption_review_block(tickets)
    assert "Assumed X" in block
    assert "`t1`" in block


def test_review_block_attributes_each_assumption_to_its_ticket():
    tickets = [
        SimpleNamespace(uid="t1", assumptions=[_a("A1")]),
        SimpleNamespace(uid="t2", assumptions=[_a("A2")]),
    ]
    block = build_assumption_review_block(tickets)
    assert "`t1` A1" in block
    assert "`t2` A2" in block


def test_review_block_demands_a_structured_verdict_and_escalation():
    block = build_assumption_review_block(
        [SimpleNamespace(uid="t1", assumptions=[_a("A")])]
    )
    assert "assumption_results" in block
    assert "confirmed|refuted|unverifiable" in block
    assert "new_blocking_findings" in block


def test_a_ticket_without_the_attribute_is_tolerated():
    """Rows predating the field read as None, not []."""
    assert build_assumption_review_block([SimpleNamespace(uid="t1", assumptions=None)]) == ""
