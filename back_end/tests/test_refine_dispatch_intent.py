"""Ticket refine intent contract — pure builder for the read-only refine run.

The intent is the whole promise the refine run makes: read the code, sharpen
the ticket via the platform tools, and never modify anything. Any drift in
those clauses would let the agent write code, or forget to persist its
conclusions, so the shape is worth pinning.
"""

from domains.tickets.models import Ticket
from domains.tickets.services.refine_dispatch import build_ticket_refine_intent


def _ticket(**over) -> Ticket:
    base = dict(
        uid="t1",
        repository_uid="repo1",
        title="Fix login flakiness",
        description="Users report intermittent 401s on login",
        priority="high",
        acceptance_criteria=["login succeeds on first try", "no 401 with a valid cookie"],
    )
    base.update(over)
    return Ticket(**base)


def test_intent_pins_the_ticket_uid_and_current_facets():
    intent = build_ticket_refine_intent(_ticket())
    assert "Ticket uid: t1" in intent
    assert "Title: Fix login flakiness" in intent
    assert "Priority: high" in intent
    assert "Users report intermittent 401s on login" in intent
    # The acceptance list is rendered — a subsequent run compares against it.
    assert "- login succeeds on first try" in intent
    assert "- no 401 with a valid cookie" in intent


def test_intent_is_explicitly_read_only():
    """Refine writes to the ticket, never to the code. This clause is the
    guardrail — a drop-through would let the agent commit files."""
    intent = build_ticket_refine_intent(_ticket())
    assert "read-only" in intent
    assert "do not modify any code" in intent.lower()


def test_intent_names_the_update_tool_with_the_ticket_uid():
    """Every write must go through the platform tools — an agent replying with
    the improved text is a plan that is not written back."""
    intent = build_ticket_refine_intent(_ticket())
    assert "opensweep_platform_update_ticket" in intent
    assert "`t1`" in intent
    assert "acceptance_criteria" in intent


def test_intent_requires_the_plan_artifact():
    """The refine run also attaches a plan — the ticket needs both the sharpened
    text and the concrete steps a developer will follow."""
    intent = build_ticket_refine_intent(_ticket())
    assert "opensweep_platform_attach_artifact" in intent
    assert "plan" in intent


def test_intent_forbids_moving_status_across_gate_one():
    """Gate 1 is human-only. A refine run promoting a ticket into `todo` would
    be self-approval of its own analysis."""
    intent = build_ticket_refine_intent(_ticket())
    assert "not change the ticket's status" in intent.lower()
    assert "Gate 1" in intent


def test_intent_falls_back_when_description_and_criteria_are_absent():
    """A ticket that was created with only a title still refines — the intent
    fills the gaps with parenthetical placeholders instead of an empty section
    the agent might read as "already done"."""
    intent = build_ticket_refine_intent(
        _ticket(description="", acceptance_criteria=[])
    )
    assert "(not provided)" in intent
    assert "(none yet)" in intent


def test_intent_handles_none_acceptance_criteria():
    """Older tickets have None on the JSON property; joining None would raise."""
    intent = build_ticket_refine_intent(_ticket(acceptance_criteria=None))
    assert "(none yet)" in intent
