"""Pure tests for the cron schedule scanner's due-time logic."""

from datetime import datetime, timezone

import pytest

from domains.agents.services.schedule_scanner import is_due


def test_is_due_fires_when_previous_cron_tick_is_after_last_run():
    # 09:00 every weekday. We're at 09:30 Monday, last run was Friday.
    now = datetime(2026, 5, 25, 9, 30, tzinfo=timezone.utc)  # Monday
    last = datetime(2026, 5, 22, 9, 5, tzinfo=timezone.utc)  # last Friday's fire
    assert is_due("0 9 * * 1-5", last=last, now=now) is True


def test_is_due_skips_when_already_dispatched_this_minute():
    now = datetime(2026, 5, 25, 9, 30, tzinfo=timezone.utc)
    last = datetime(2026, 5, 25, 9, 10, tzinfo=timezone.utc)  # already after the 09:00 fire
    assert is_due("0 9 * * *", last=last, now=now) is False


def test_is_due_first_run_fires_immediately():
    now = datetime(2026, 5, 25, 9, 30, tzinfo=timezone.utc)
    assert is_due("0 9 * * *", last=None, now=now) is True


def test_is_due_rejects_invalid_expression():
    with pytest.raises(ValueError):
        is_due("not a cron", last=None, now=datetime.now(timezone.utc))
