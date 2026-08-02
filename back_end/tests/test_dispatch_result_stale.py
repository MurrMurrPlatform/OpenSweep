"""Lifecycle discard rule — pure part, no DB required.

Regression guard for the bug where a CLI implement/fix agent that
self-completed its first turn via the `complete_run` platform tool had its
adapter result discarded, skipping the write-gate push + draft PR. The agent
committed in the sandbox but nothing was ever pushed.
"""

from domains.runs.schemas import RunStatus
from domains.runs.services.lifecycle import dispatch_result_is_stale


def test_agent_self_completed_turn_is_not_stale():
    # The self-report status (complete_run's default) must finalize, not be
    # discarded — otherwise on_turn_complete never pushes the work branch.
    assert dispatch_result_is_stale(RunStatus.AWAITING_INPUT.value) is False


def test_still_running_result_is_not_stale():
    # Adapter finished without the agent self-reporting: the lifecycle's own
    # complete_run finalizes it.
    assert dispatch_result_is_stale(RunStatus.RUNNING.value) is False


def test_outside_termination_is_stale():
    # Human cancel/end or the reconciler failing the run: those paths fire the
    # completion hook themselves; re-finalizing would resurrect a dead run.
    assert dispatch_result_is_stale(RunStatus.CANCELLED.value) is True
    assert dispatch_result_is_stale(RunStatus.FAILED.value) is True
    assert dispatch_result_is_stale(RunStatus.ENDED.value) is True


def test_other_non_running_states_stay_stale():
    # Unchanged from the original guard: only running/awaiting_input proceed.
    assert dispatch_result_is_stale(RunStatus.QUEUED.value) is True
    assert dispatch_result_is_stale(RunStatus.LIMIT_EXCEEDED.value) is True
    assert dispatch_result_is_stale(RunStatus.PAUSED_QUOTA.value) is True


def test_agent_self_completed_terminal_status_is_not_stale():
    # complete_run(final_status="failed"/"ended"/"limit_exceeded") records
    # self_reported_status; that is the agent finishing this turn, so finalize
    # must run (its commit still needs the write-gate push).
    for s in (RunStatus.FAILED.value, RunStatus.ENDED.value, RunStatus.LIMIT_EXCEEDED.value):
        assert dispatch_result_is_stale(s, self_reported_status=s) is False


def test_outside_kill_with_stale_self_report_is_still_stale():
    # An outside cancel while usage still carries a PRIOR turn's self-report
    # ("awaiting_input") must NOT be mistaken for an agent self-completion.
    assert (
        dispatch_result_is_stale(
            RunStatus.CANCELLED.value, self_reported_status=RunStatus.AWAITING_INPUT.value
        )
        is True
    )
