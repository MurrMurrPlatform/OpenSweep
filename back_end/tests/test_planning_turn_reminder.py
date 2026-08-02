"""The planning-stage reminder is appended at most once per message.

`TurnService.run_turn` staples the reminder onto every turn while a thread is
refining. The guard has to be idempotent across *wordings*, not just against
the one constant this module happens to export — the reminder body varies by
autonomy tier, so an equality check would let a second, contradictory reminder
through on a re-send.
"""

from domains.threads.services.intents import (
    PLANNING_TURN_REMINDER,
    REMINDER_SENTINEL,
    needs_planning_reminder,
)


def test_sentinel_prefixes_the_shipped_reminder():
    """The invariant the guard rests on. If the reminder is ever reworded so it
    no longer starts with the sentinel, the guard silently stops matching and
    every turn gets a duplicate — so assert it rather than trusting it."""
    assert PLANNING_TURN_REMINDER.startswith(REMINDER_SENTINEL)


def test_plain_message_needs_the_reminder():
    assert needs_planning_reminder("Use the existing helper, please.")


def test_empty_and_none_need_the_reminder():
    assert needs_planning_reminder("")
    assert needs_planning_reminder(None)


def test_message_already_carrying_the_reminder_is_left_alone():
    text = f"Some answer\n\n{PLANNING_TURN_REMINDER}"
    assert not needs_planning_reminder(text)


def test_a_DIFFERENTLY_WORDED_reminder_is_still_detected():
    """The regression this guard exists for: an autonomy-tier reminder that is
    not byte-identical to PLANNING_TURN_REMINDER must still suppress the
    append. An exact-string check passes every other test in this file and
    fails this one."""
    other = (
        "[Thread protocol reminder — PLANNING stage: ask at most 3 questions; "
        "answer anything else yourself and record it.]"
    )
    assert not needs_planning_reminder(f"Some answer\n\n{other}")
