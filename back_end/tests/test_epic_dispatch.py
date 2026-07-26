"""plan_epic_dispatch decision matrix — pure."""

from datetime import UTC, datetime, timedelta

from domains.runs.services.active_runs import ACTIVE_RUN_STATUSES
from domains.tickets.services.epic_dispatch import (
    dispatch_deadline_passed,
    epic_targets,
    plan_epic_dispatch,
)


def _entry(ticket_uid, run_uid=""):
    return {"ticket_uid": ticket_uid, "run_uid": run_uid}


# ── target selection ──────────────────────────────────────────────────────


def test_single_pr_targets_the_parent_ticket_only():
    assert epic_targets("single-pr", "parent", ["m0", "m1"]) == ["parent"]


def test_parallel_runs_targets_every_member_not_the_parent():
    assert epic_targets("parallel-runs", "parent", ["m0", "m1"]) == ["m0", "m1"]


def test_unknown_shape_falls_back_to_single_pr():
    assert epic_targets("", "parent", ["m0"]) == ["parent"]


def test_single_pr_without_a_parent_has_nothing_to_run():
    assert epic_targets("single-pr", "", ["m0"]) == []


def test_parallel_runs_drops_empty_member_uids():
    assert epic_targets("parallel-runs", "parent", ["m0", "", "m1"]) == ["m0", "m1"]


# ── capacity ──────────────────────────────────────────────────────────────


def test_dispatches_up_to_max_parallel_in_target_order():
    out = plan_epic_dispatch(["t0", "t1", "t2"], [], {}, 2)
    assert out == {"dispatch": ["t0", "t1"], "complete": False, "failed": []}


def test_in_flight_runs_consume_capacity():
    out = plan_epic_dispatch(
        ["t0", "t1", "t2"], [_entry("t0", "r0")], {"r0": "running"}, 2
    )
    assert out["dispatch"] == ["t1"]


def test_capacity_zero_dispatches_nothing():
    out = plan_epic_dispatch(
        ["t0", "t1", "t2"],
        [_entry("t0", "r0"), _entry("t1", "r1")],
        {"r0": "running", "r1": "queued"},
        2,
    )
    assert out["dispatch"] == []
    assert out["complete"] is False


def test_max_parallel_zero_dispatches_nothing():
    out = plan_epic_dispatch(["t0"], [], {}, 0)
    assert out["dispatch"] == []
    assert out["complete"] is False


def test_negative_max_parallel_is_treated_as_zero():
    out = plan_epic_dispatch(["t0"], [], {}, -5)
    assert out["dispatch"] == []


# ── already dispatched ────────────────────────────────────────────────────


def test_already_dispatched_targets_are_never_redispatched():
    """The `dispatched` record — not the run's status — is what stops a
    second write run being started for a member."""
    entries = [_entry("t0", "r0")]
    for status in ("running", "ended", "failed", "awaiting_input"):
        out = plan_epic_dispatch(["t0", "t1"], entries, {"r0": status}, 5)
        assert "t0" not in out["dispatch"], status


def test_duplicate_dispatch_entries_count_once():
    """A double-recorded member must not eat two capacity slots."""
    entries = [_entry("t0", "r0"), _entry("t0", "r0")]
    out = plan_epic_dispatch(["t0", "t1"], entries, {"r0": "running"}, 2)
    assert out["dispatch"] == ["t1"]


def test_duplicate_targets_are_dispatched_once():
    out = plan_epic_dispatch(["t0", "t0", "t1"], [], {}, 5)
    assert out["dispatch"] == ["t0", "t1"]


def test_no_targets_is_immediately_complete():
    out = plan_epic_dispatch([], [], {}, 3)
    assert out == {"dispatch": [], "complete": True, "failed": []}


# ── in-flight accounting ──────────────────────────────────────────────────


def test_every_active_run_status_counts_as_in_flight():
    for status in sorted(ACTIVE_RUN_STATUSES):
        out = plan_epic_dispatch(
            ["t0", "t1"], [_entry("t0", "r0")], {"r0": status}, 1
        )
        assert out["dispatch"] == [], status
        assert out["complete"] is False, status
        assert out["failed"] == [], status


def test_paused_quota_counts_as_in_flight():
    # The resume beat re-dispatches that very run, so it still owns a slot.
    out = plan_epic_dispatch(["t0"], [_entry("t0", "r0")], {"r0": "paused_quota"}, 3)
    assert out["complete"] is False
    assert out["failed"] == []


def test_finished_runs_free_capacity():
    entries = [_entry("t0", "r0"), _entry("t1", "r1")]
    out = plan_epic_dispatch(
        ["t0", "t1", "t2"], entries, {"r0": "ended", "r1": "awaiting_input"}, 2
    )
    assert out["dispatch"] == ["t2"]


def test_vanished_run_counts_as_finished_not_in_flight():
    """A run row that is gone can never come back — counting it in flight
    would keep the epic dispatching forever."""
    out = plan_epic_dispatch(["t0", "t1"], [_entry("t0", "gone")], {}, 1)
    assert out["dispatch"] == ["t1"]
    assert out["failed"] == ["t0"]


def test_member_recorded_without_a_run_uid_is_not_in_flight():
    # How the applier records a target it could never start (deleted ticket).
    out = plan_epic_dispatch(["t0"], [_entry("t0", "")], {}, 3)
    assert out["complete"] is True
    assert out["failed"] == ["t0"]


def test_entries_without_a_ticket_uid_are_ignored():
    out = plan_epic_dispatch(["t0"], [{"run_uid": "r9"}], {"r9": "running"}, 1)
    assert out["dispatch"] == ["t0"]


# ── failed reporting ──────────────────────────────────────────────────────


def test_terminal_failure_statuses_are_reported():
    entries = [_entry("t0", "r0"), _entry("t1", "r1"), _entry("t2", "r2")]
    statuses = {"r0": "failed", "r1": "cancelled", "r2": "limit_exceeded"}
    out = plan_epic_dispatch(["t0", "t1", "t2"], entries, statuses, 3)
    assert out["failed"] == ["t0", "t1", "t2"]


def test_a_failed_member_does_not_block_completion():
    """Dispatching was the epic's job; a member whose agent gave up is
    visible on the ticket and the run, not by parking the epic."""
    out = plan_epic_dispatch(["t0"], [_entry("t0", "r0")], {"r0": "failed"}, 3)
    assert out["complete"] is True
    assert out["failed"] == ["t0"]


def test_successful_runs_are_never_reported_as_failed():
    entries = [_entry("t0", "r0"), _entry("t1", "r1")]
    out = plan_epic_dispatch(
        ["t0", "t1"], entries, {"r0": "ended", "r1": "awaiting_input"}, 3
    )
    assert out["failed"] == []


# ── completion ────────────────────────────────────────────────────────────


def test_complete_only_when_all_dispatched_and_none_in_flight():
    targets = ["t0", "t1"]
    # Nothing started yet.
    assert plan_epic_dispatch(targets, [], {}, 2)["complete"] is False
    # Half started, and it is still running.
    part = [_entry("t0", "r0")]
    assert plan_epic_dispatch(targets, part, {"r0": "running"}, 2)["complete"] is False
    # Half started and finished — the other half never was.
    assert plan_epic_dispatch(targets, part, {"r0": "ended"}, 2)["complete"] is False
    # All started, one still in flight.
    both = [_entry("t0", "r0"), _entry("t1", "r1")]
    statuses = {"r0": "ended", "r1": "queued"}
    assert plan_epic_dispatch(targets, both, statuses, 2)["complete"] is False
    # All started, all finished.
    statuses = {"r0": "ended", "r1": "awaiting_input"}
    assert plan_epic_dispatch(targets, both, statuses, 2)["complete"] is True


def test_capacity_starvation_does_not_read_as_complete():
    """Zero headroom must stall the epic, not retire it."""
    out = plan_epic_dispatch(["t0", "t1"], [], {}, 5, 0)
    assert out["dispatch"] == []
    assert out["complete"] is False


def test_extra_dispatched_entries_outside_targets_do_not_prevent_completion():
    # A member dropped from the epic after its run started still counts
    # against in-flight capacity, but never keeps the epic open once done.
    entries = [_entry("t0", "r0"), _entry("stale", "r9")]
    out = plan_epic_dispatch(["t0"], entries, {"r0": "ended", "r9": "ended"}, 2)
    assert out["complete"] is True


def test_stale_entry_still_in_flight_holds_a_capacity_slot():
    entries = [_entry("stale", "r9")]
    out = plan_epic_dispatch(["t0", "t1"], entries, {"r9": "running"}, 2)
    assert out["dispatch"] == ["t0"]


# ── provider headroom clamp ───────────────────────────────────────────────


def test_provider_headroom_clamps_below_max_parallel():
    """The tighter of the two ceilings wins — an epic allowed 5 runs still
    only gets what the provider has capacity for."""
    out = plan_epic_dispatch(["t0", "t1", "t2", "t3", "t4"], [], {}, 5, 2)
    assert out["dispatch"] == ["t0", "t1"]


def test_max_parallel_clamps_below_provider_headroom():
    out = plan_epic_dispatch(["t0", "t1", "t2", "t3", "t4"], [], {}, 2, 99)
    assert out["dispatch"] == ["t0", "t1"]


def test_zero_provider_headroom_dispatches_nothing():
    """A saturated provider stalls dispatch rather than piling on runs that
    would only park in paused_quota."""
    out = plan_epic_dispatch(["t0", "t1"], [], {}, 5, 0)
    assert out["dispatch"] == []


def test_unknown_provider_headroom_does_not_clamp():
    """None = couldn't resolve a provider; fall back to the epic cap alone
    rather than silently stalling the epic."""
    out = plan_epic_dispatch(["t0", "t1", "t2"], [], {}, 3, None)
    assert out["dispatch"] == ["t0", "t1", "t2"]


def test_negative_provider_headroom_is_treated_as_zero():
    out = plan_epic_dispatch(["t0"], [], {}, 3, -1)
    assert out["dispatch"] == []


def test_headroom_already_accounts_for_own_in_flight_runs():
    """provider_headroom counts this epic's own runs, so it must NOT be
    reduced a second time by in_flight — the two ceilings are independent."""
    entries = [_entry("t0", "r0")]
    # 1 run in flight: epic capacity 5-1=4, provider headroom already 4.
    out = plan_epic_dispatch(
        ["t0", "t1", "t2"], entries, {"r0": "running"}, 5, 4
    )
    assert out["dispatch"] == ["t1", "t2"]


# ── determinism ───────────────────────────────────────────────────────────


def test_plan_is_deterministic_and_order_stable():
    targets = ["z", "a", "m", "b"]
    entries = [_entry("m", "rm")]
    statuses = {"rm": "running"}
    first = plan_epic_dispatch(targets, entries, statuses, 4, 3)
    for _ in range(5):
        assert plan_epic_dispatch(targets, entries, statuses, 4, 3) == first
    # Target order is the epic's own order, not sorted.
    assert first["dispatch"] == ["z", "a", "b"]


def test_plan_does_not_mutate_its_inputs():
    targets = ["t0", "t1"]
    entries = [_entry("t0", "r0")]
    statuses = {"r0": "running"}
    plan_epic_dispatch(targets, entries, statuses, 2, 1)
    assert targets == ["t0", "t1"]
    assert entries == [{"ticket_uid": "t0", "run_uid": "r0"}]
    assert statuses == {"r0": "running"}


# ── Dispatch deadline ───────────────────────────────────────────────────────
#
# WHY: `trigger_implement_run` raises 409 both for a TRANSIENT conflict (a
# write run is already in flight — retry next tick, correct) and a PERMANENT
# one (an open PR already implements this ticket, or a human picked it up
# outside the epic). A permanently-409ing member never enters `dispatched`,
# so `complete` never becomes true: without a deadline the epic retries every
# minute forever while reporting `dispatching`, i.e. claiming work is still
# coming when none ever will be.


def test_deadline_not_passed_before_the_window():
    started = datetime(2026, 7, 26, 0, 0, tzinfo=UTC)
    assert not dispatch_deadline_passed(started, started + timedelta(hours=23))


def test_deadline_passes_after_the_window():
    started = datetime(2026, 7, 26, 0, 0, tzinfo=UTC)
    assert dispatch_deadline_passed(started, started + timedelta(hours=25))


def test_deadline_is_exclusive_at_the_boundary():
    """Exactly at the deadline is not yet past it — one more tick is free."""
    started = datetime(2026, 7, 26, 0, 0, tzinfo=UTC)
    assert not dispatch_deadline_passed(started, started + timedelta(hours=24))


def test_missing_start_time_never_expires():
    """Rows predating the field must not be failed the moment a tick sees
    them — the deadline stops infinite retries, it does not fail unknown work."""
    assert not dispatch_deadline_passed(None, datetime.now(UTC))


def test_naive_start_time_is_treated_as_utc():
    """neomodel can hand back a naive datetime; comparing it to an aware `now`
    would raise TypeError and take down the whole tick."""
    naive = datetime(2026, 7, 26, 0, 0)
    assert dispatch_deadline_passed(naive, datetime(2026, 7, 27, 6, 0, tzinfo=UTC))
    assert not dispatch_deadline_passed(naive, datetime(2026, 7, 26, 6, 0, tzinfo=UTC))


def test_deadline_window_is_configurable():
    started = datetime(2026, 7, 26, 0, 0, tzinfo=UTC)
    now = started + timedelta(minutes=90)
    assert dispatch_deadline_passed(started, now, deadline=timedelta(hours=1))
    assert not dispatch_deadline_passed(started, now, deadline=timedelta(hours=2))
