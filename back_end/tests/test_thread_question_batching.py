"""Batched questions: pure gating helpers + combined delivery message."""

from domains.threads.services.thread_service import (
    build_answers_message,
    open_question_events,
    pending_answer_events,
)


def _q(uid, status="open", answer="", delivered=False):
    e = {
        "type": "question",
        "uid": uid,
        "question": f"Q-{uid}?",
        "status": status,
        "answer": answer,
    }
    if delivered:
        e["delivered_at"] = "2026-07-18T12:00:00+00:00"
    return e


def test_open_and_pending_partition():
    events = [
        _q("a", status="answered", answer="yes"),
        _q("b"),  # still open
        _q("c", status="answered", answer="no", delivered=True),  # already sent
        {"type": "phase_changed", "frm": "refining", "to": "implementing"},
    ]
    assert [e["uid"] for e in open_question_events(events)] == ["b"]
    assert [e["uid"] for e in pending_answer_events(events)] == ["a"]


def test_combined_message_lists_all_pairs_in_order():
    msg = build_answers_message(
        [_q("a", "answered", "yes"), _q("b", "answered", "42")]
    )
    assert msg.index("Q-a?") < msg.index("Q-b?")
    assert "A: yes" in msg and "A: 42" in msg


def test_forced_continue_names_the_skipped():
    msg = build_answers_message([_q("a", "answered", "yes")], skipped=[_q("b")])
    assert "CONTINUE WITHOUT ANSWERING" in msg
    assert "Q-b?" in msg and "best judgment" in msg
