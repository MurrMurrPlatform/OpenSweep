from domains.executors._shared import budget_briefing


class _P:
    max_tool_turns = 200
    max_dollars = 20.0
    warn_at_pct = 80


class _Unlimited:
    max_tool_turns = None
    max_dollars = None
    warn_at_pct = 80


def test_bounded_policy_renders_numbers_and_winddown_rule():
    text = budget_briefing(_P(), 3600)
    assert "60 minutes" in text
    assert "200 tool turns" in text
    assert "80%" in text
    assert "complete_run" in text


def test_unlimited_policy_renders_unbounded_briefing():
    text = budget_briefing(_Unlimited(), None)
    assert "no fixed budget" in text
    assert "complete_run" in text


def test_no_policy_no_wall_is_still_nonempty():
    assert "complete_run" in budget_briefing(None, None)
