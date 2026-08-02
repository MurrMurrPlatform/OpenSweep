"""Batch triage: budget, intent contract, and the policy-question test.

The value of batching is question DEDUPLICATION — thirty tickets raise a
handful of distinct policy questions, and the human should answer each once.
Most of that is prompt-level and untestable here; what IS testable is the
budget rule, the ordering instruction that makes dedup possible at all, and the
server-side pressure that stops a local question being dressed up as a policy.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from domains.runs.schemas import Effort
from domains.tickets.services.batch_triage import (
    MAX_BATCH,
    build_batch_triage_intent,
    effort_for_batch,
)


def _fact(uid, title="T", paths=(), desc="d"):
    return SimpleNamespace(
        uid=uid, title=title, description=desc, paths=list(paths), priority="high"
    )


def test_small_batches_get_the_normal_ceiling():
    assert effort_for_batch(1) is Effort.NORMAL
    assert effort_for_batch(10) is Effort.NORMAL


def test_large_batches_get_the_deep_ceiling():
    """One run over 25 tickets needs the ceiling of 25 runs, not of one — at
    ~6 tool calls per ticket a NORMAL policy's 200-turn cap is spent before the
    reading the intent demands is done."""
    assert effort_for_batch(11) is Effort.DEEP
    assert effort_for_batch(MAX_BATCH) is Effort.DEEP


def test_the_intent_lists_every_candidate_by_uid():
    intent = build_batch_triage_intent(
        [_fact("t1"), _fact("t2")], [], repository_uid="r1"
    )
    assert "`t1`" in intent and "`t2`" in intent


def test_the_intent_demands_classify_before_asking():
    """This ordering IS the dedup mechanism. Ask-as-you-go produces the same
    question at ticket 3 and again, reworded, at ticket 17."""
    intent = build_batch_triage_intent([_fact("t1")], [], repository_uid="r1")
    assert "BEFORE asking any of them" in intent


def test_the_intent_defines_policy_as_two_or_more_tickets():
    intent = build_batch_triage_intent([_fact("t1")], [], repository_uid="r1")
    assert "TWO OR MORE" in intent


def test_the_intent_tells_the_agent_to_answer_local_questions_itself():
    intent = build_batch_triage_intent([_fact("t1")], [], repository_uid="r1")
    assert "record_assumption" in intent


def test_the_intent_asks_for_a_resumable_skip_list():
    """Partial progress is already safe — every write commits immediately — but
    without this the run cannot say WHERE it stopped."""
    intent = build_batch_triage_intent([_fact("t1")], [], repository_uid="r1")
    assert "complete_run.skipped" in intent


def test_known_policies_are_seeded_and_marked_answered():
    policies = [SimpleNamespace(title="Policy: errors", body="Raise, don't log")]
    intent = build_batch_triage_intent([_fact("t1")], policies, repository_uid="r1")
    assert "Policy: errors" in intent
    assert "Do NOT ask any of these again" in intent


def test_an_empty_policy_list_says_so_rather_than_rendering_nothing():
    """Silence would read as "no policies apply"; the agent should know it is
    the first batch and questions are expected."""
    intent = build_batch_triage_intent([_fact("t1")], [], repository_uid="r1")
    assert "No policies have been recorded" in intent


@pytest.mark.asyncio
async def test_a_policy_question_naming_one_ticket_is_refused():
    """The ≥2 rule is the ONLY server-side pressure toward classifying before
    asking. Without it "policy" is a label the agent awards itself."""
    from domains.platform_tools.ask_policy_question import ask_policy_question

    with pytest.raises(HTTPException) as exc:
        await ask_policy_question(
            question="Which error type?",
            applies_to_ticket_uids=["t1"],
            executor="run-1",
        )
    assert exc.value.status_code == 422
    assert "record_assumption" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_duplicate_uids_do_not_fake_a_policy_question():
    from domains.platform_tools.ask_policy_question import ask_policy_question

    with pytest.raises(HTTPException) as exc:
        await ask_policy_question(
            question="Which error type?",
            applies_to_ticket_uids=["t1", "t1", "t1"],
            executor="run-1",
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_a_policy_question_outside_a_run_is_refused():
    from domains.platform_tools.ask_policy_question import ask_policy_question

    with pytest.raises(HTTPException) as exc:
        await ask_policy_question(
            question="q", applies_to_ticket_uids=["t1", "t2"], executor="manual"
        )
    assert exc.value.status_code == 422
