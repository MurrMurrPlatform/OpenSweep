"""Thread runs may not post comments — structural guard, not prompt-level.

Dogfooding (tickets f6bb30bd, e2ccaa97) showed agents route plans/questions
to add_comment regardless of intent instructions; the tool now rejects
thread-playbook runs with a rejection message that teaches the correct
tools mid-run.
"""

from api.v1.platform_tools_comments import (
    THREAD_COMMENT_REJECTION,
    thread_comment_error,
)
from domains.threads.services.intents import build_thread_session_intent


def test_thread_playbook_is_rejected():
    assert thread_comment_error("thread") == THREAD_COMMENT_REJECTION


def test_other_playbooks_may_comment():
    for playbook in ("chat", "ask", "review", "fix", "implement", "refine", ""):
        assert thread_comment_error(playbook) is None


def test_rejection_teaches_the_replacement_tools():
    assert "opensweep_platform_ask_user" in THREAD_COMMENT_REJECTION
    assert "opensweep_platform_submit_thread_plan" in THREAD_COMMENT_REJECTION


def test_intent_declares_comments_disabled():
    from types import SimpleNamespace

    ticket = SimpleNamespace(
        uid="t-1", title="T", description="", acceptance_criteria=[], priority="medium"
    )
    intent = build_thread_session_intent(ticket, "th-1")
    assert "DISABLED" in intent and "add_comment" in intent
