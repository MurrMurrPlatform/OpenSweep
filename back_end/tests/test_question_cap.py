"""Server-side question cap on ask_user.

Enforced in code, not just in the prompt, because the failure is asymmetric: an
agent that overshoots a FINDING cap files a few extra findings; one that
overshoots a QUESTION cap ends its turn N times and leaves a human with N
answer cards on a ticket that is going nowhere — the exact state `assume`
exists to prevent.
"""

from domains.platform_tools.ask_user import asked_question_count, question_cap_refusal


def _q(status="open"):
    return {"type": "question", "status": status, "question": "why?"}


def test_count_is_lifetime_not_per_turn():
    """Per-turn counting would let the agent ask K questions every turn
    forever, which is not a cap at all."""
    events = [_q(), _q("answered"), {"type": "user_message"}, _q("dismissed")]
    assert asked_question_count(events) == 3


def test_interrogate_is_uncapped():
    events = [_q() for _ in range(50)]
    assert question_cap_refusal(events, autonomy="interrogate") is None


def test_empty_autonomy_is_uncapped_like_today():
    """Threads predating the dial store "" and must not suddenly be muzzled."""
    assert question_cap_refusal([_q() for _ in range(9)], autonomy="") is None


def test_assume_allows_up_to_three_then_refuses():
    for n in (0, 1, 2):
        assert question_cap_refusal([_q()] * n, autonomy="assume") is None
    refusal = question_cap_refusal([_q()] * 3, autonomy="assume")
    assert refusal is not None
    assert refusal["asked"] == 3 and refusal["cap"] == 3


def test_strict_refuses_the_very_first_question():
    refusal = question_cap_refusal([], autonomy="strict")
    assert refusal is not None
    assert refusal["cap"] == 0


def test_the_refusal_redirects_to_the_ledger_rather_than_just_saying_no():
    """The payload IS the instruction — this is what turns a blocked question
    into a recorded assumption instead of a stalled turn."""
    refusal = question_cap_refusal([_q()] * 3, autonomy="assume")
    assert "opensweep_platform_record_assumption" in refusal["instruction"]
    assert "answer it" in refusal["instruction"].lower()


def test_the_refusal_shape_matches_a_normal_ask_user_result():
    """Callers destructure the same keys either way, so a capped call cannot
    KeyError its way into a crashed turn."""
    refusal = question_cap_refusal([_q()] * 3, autonomy="assume")
    assert set(refusal) >= {"status", "question_uid"}
    assert refusal["status"] == "capped"
    assert refusal["question_uid"] == ""


def test_an_unknown_tier_does_not_silently_muzzle_the_agent():
    """Failing safe here means asking MORE, never less."""
    assert question_cap_refusal([_q()] * 9, autonomy="bananas") is None
