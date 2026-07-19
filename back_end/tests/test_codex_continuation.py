"""Codex transcript-tail continuation pass (Task 7).

Pure tests for codex_continuation_prompt — no Neo4j, no I/O, no CLI.
"""

from domains.executors.cli_tracking import (
    codex_continuation_prompt,
    envelope_has_complete_run,
)


def test_continuation_prompt_embeds_tail_and_nudge():
    out = codex_continuation_prompt("CONTINUE NOW", "assistant: found 3 issues so far")
    assert "CONTINUE NOW" in out
    assert "found 3 issues so far" in out
    assert "no session resume" in out


def test_envelope_has_complete_run_true_when_present():
    envelope = {
        "summary": "done",
        "tool_calls": [
            {"tool": "create_finding", "args": {}},
            {"tool": "complete_run", "args": {"summary": "wrapped up"}},
        ],
    }
    assert envelope_has_complete_run(envelope) is True


def test_envelope_has_complete_run_false_without_entry():
    envelope = {"summary": "still going", "tool_calls": [{"tool": "create_finding", "args": {}}]}
    assert envelope_has_complete_run(envelope) is False


def test_envelope_has_complete_run_false_on_none():
    assert envelope_has_complete_run(None) is False


def test_envelope_has_complete_run_false_on_empty_dict():
    assert envelope_has_complete_run({}) is False
