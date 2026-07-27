"""Celery wiring — beat entries exist and long-running work carries its own
time limits (a resumed CLI run must not run inside the global 600/900s box).
"""

import celery_app
import domains.runs.tasks.resume_paused as resume_paused_module  # noqa: F401 — registers tasks
from celery_app import app


def test_sandbox_cleanup_is_scheduled_every_30_minutes():
    beat = app.conf.beat_schedule
    entry = beat.get("sandbox-cleanup")
    assert entry is not None, "cleanup_sandboxes task existed but was never scheduled"
    assert entry["task"] == "opensweep.execution.cleanup_sandboxes"
    assert entry["schedule"] == 1800.0


def test_resume_beat_entry_still_scheduled():
    entry = app.conf.beat_schedule.get("run-resume-paused")
    assert entry is not None
    assert entry["task"] == "opensweep.runs.resume_paused_runs"


def test_resume_run_task_registered_with_long_limits():
    """The registered limits are the FALLBACK for an invocation without
    explicit ones — `_scan_and_enqueue` passes policy-derived limits per run
    (domains/runs/tasks/task_limits.py). What matters here is that the per-run
    task exists and is bounded far above the global tick limits, not the
    specific numbers.
    """
    from domains.runs.tasks.task_limits import (
        DEFAULT_DISPATCH_SECONDS,
        dispatch_time_limits,
    )

    task = app.tasks.get("opensweep.runs.resume_run")
    assert task is not None, "per-run resume task missing — beat tick would redispatch inline"

    expected_soft, expected_hard = dispatch_time_limits(DEFAULT_DISPATCH_SECONDS)
    assert task.soft_time_limit == expected_soft
    assert task.time_limit == expected_hard
    # The whole point of a per-run task: it outlives the beat tick's limits.
    assert task.soft_time_limit > celery_app.app.conf.task_soft_time_limit
    assert task.time_limit > celery_app.app.conf.task_time_limit


def test_global_limits_unchanged_for_ticks():
    assert celery_app.app.conf.task_soft_time_limit == 600
    assert celery_app.app.conf.task_time_limit == 900


def test_beat_scan_task_registered():
    assert app.tasks.get("opensweep.runs.resume_paused_runs") is not None


def test_every_beat_entry_resolves_to_a_registered_task():
    """Generic guard: a beat entry naming a task celery never imports fails at
    RUNTIME with "Received unregistered task", which shows up as a silently
    dead schedule rather than a failed boot. Adding a task module to
    `beat_schedule` but forgetting `include=[...]` is the easy way to do this,
    so assert the whole schedule rather than one entry at a time.
    """
    app.loader.import_default_modules()  # what a worker does on start
    missing = {
        name: entry["task"]
        for name, entry in app.conf.beat_schedule.items()
        if entry["task"] not in app.tasks
    }
    assert not missing, f"beat entries naming unregistered tasks: {missing}"




def test_campaign_tick_is_scheduled_every_minute():
    import domains.campaigns.tasks.campaign_tick as campaign_tick_module  # noqa: F401 — registers task

    entry = app.conf.beat_schedule.get("campaign-tick")
    assert entry is not None, "campaign tick must be beat-scheduled or parts never chain"
    assert entry["task"] == "opensweep.campaigns.tick"
    assert entry["schedule"] == 60.0
    assert app.tasks.get("opensweep.campaigns.tick") is not None
