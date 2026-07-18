"""Decision-log distillation: planning conversation in, markdown log out.

Deterministic v1 (spec: 'structured carry-over always'): every user message
is a decision/answer worth carrying verbatim; the final assistant message is
the agent's own closing summary. Tool spam is excluded.
"""

from domains.threads.services.decision_log import build_decision_log


def _ev(type: str, text: str, seq: int) -> dict:
    return {"seq": seq, "ts": "2026-07-18T10:00:00Z", "turn": 1, "type": type, "text": text}


def test_includes_all_user_messages_in_order():
    events = [
        _ev("user_message", "Use Redis pub/sub, not polling", 1),
        _ev("assistant_text", "Understood. Should the gate be soft?", 2),
        _ev("user_message", "Yes, soft gate", 3),
    ]
    log = build_decision_log(events)
    assert log.index("Use Redis pub/sub") < log.index("Yes, soft gate")


def test_includes_only_final_assistant_text():
    events = [
        _ev("assistant_text", "First thought", 1),
        _ev("user_message", "ok", 2),
        _ev("assistant_text", "Final summary of the plan", 3),
    ]
    log = build_decision_log(events)
    assert "Final summary of the plan" in log
    assert "First thought" not in log


def test_excludes_tool_events():
    events = [
        _ev("user_message", "go", 1),
        {"seq": 2, "ts": "", "turn": 1, "type": "tool_use", "name": "read_doc", "input": {}},
    ]
    log = build_decision_log(events)
    assert "read_doc" not in log


def test_empty_events_gives_empty_string():
    assert build_decision_log([]) == ""


def test_truncates_oldest_first_when_over_budget():
    events = [
        _ev("user_message", "OLD " * 100, 1),
        _ev("user_message", "KEEP-THIS-RECENT-DECISION", 2),
    ]
    log = build_decision_log(events, max_chars=120)
    assert "KEEP-THIS-RECENT-DECISION" in log
    assert len(log) <= 200  # header + budgeted body
