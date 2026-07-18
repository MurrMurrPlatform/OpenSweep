"""Ticket.plan metadata: model field + DTO passthrough + prompt guard."""

from types import SimpleNamespace

from domains.threads.services.intents import build_thread_session_intent
from domains.tickets.models import Ticket
from domains.tickets.services.ticket_service import ticket_to_dto


def test_ticket_has_plan_json_field():
    props = Ticket.defined_properties(rels=False, aliases=False)
    assert "plan" in props


def test_dto_carries_plan():
    t = SimpleNamespace(
        uid="t-1", repository_uid="r-1", title="T", description="",
        acceptance_criteria=[], labels=[], status="todo", priority="medium",
        size="", origin="human", origin_finding_uid="", parent_ticket_uid="",
        linked_finding_uids=[], linked_pr_uids=[], assignee_uid="",
        plan={"markdown": "## P", "state": "drafted", "thread_uid": "th-1"},
        approved_by="", approved_at=None, done_at=None, created_at=None,
        updated_at=None,
    )
    dto = ticket_to_dto(t)
    assert dto.plan["markdown"] == "## P"
    assert dto.plan["state"] == "drafted"


def test_session_intent_forbids_comment_side_channel():
    ticket = SimpleNamespace(
        uid="t-1", title="T", description="", acceptance_criteria=[], priority="medium"
    )
    intent = build_thread_session_intent(ticket, "th-1")
    # Observed failure: the agent posted its question and plan as ticket
    # COMMENTS, so submit_thread_plan never fired and the plan stayed empty.
    # The ban is structural now (test_thread_comment_guard); the prompt
    # declares it so the agent isn't surprised by rejections.
    assert "add_comment" in intent and "DISABLED" in intent
    assert "ONLY place the platform reads it from" in intent
