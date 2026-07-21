"""plan_tick decision matrix — pure."""

from domains.campaigns.services.tick import plan_tick


def _part(idx, *, kind="area", state="pending", run_uid=""):
    return {
        "idx": idx,
        "kind": kind,
        "title": f"p{idx}",
        "scope_paths": [f"p{idx}"],
        "doc_uids": [],
        "lens_keys": ["bugs"],
        "run_uid": run_uid,
        "state": state,
        "file_count": 10,
        "area_keys": [],
    }


def test_dispatches_up_to_max_parallel_in_idx_order():
    parts = [_part(0), _part(1), _part(2)]
    out = plan_tick(parts, {}, 2)
    assert out == {
        "mark_done": [],
        "mark_failed": [],
        "dispatch": [0, 1],
        "complete": False,
    }


def test_in_flight_runs_consume_capacity():
    parts = [_part(0, state="running", run_uid="r0"), _part(1), _part(2)]
    out = plan_tick(parts, {"r0": "running"}, 2)
    assert out["dispatch"] == [1]


def test_paused_quota_counts_as_in_flight():
    parts = [_part(0, state="running", run_uid="r0"), _part(1)]
    out = plan_tick(parts, {"r0": "paused_quota"}, 1)
    assert out["mark_done"] == [] and out["mark_failed"] == []
    assert out["dispatch"] == []
    assert out["complete"] is False


def test_terminal_run_statuses_map_to_part_states():
    parts = [
        _part(0, state="running", run_uid="r0"),
        _part(1, state="running", run_uid="r1"),
        _part(2, state="running", run_uid="r2"),
        _part(3, state="running", run_uid="r3"),
        _part(4, state="running", run_uid="r4"),
    ]
    statuses = {
        "r0": "awaiting_input",
        "r1": "ended",
        "r2": "failed",
        "r3": "cancelled",
        "r4": "limit_exceeded",
    }
    out = plan_tick(parts, statuses, 2)
    assert out["mark_done"] == [0, 1]
    assert out["mark_failed"] == [2, 3, 4]


def test_running_part_with_missing_run_is_failed():
    parts = [_part(0, state="running", run_uid="gone")]
    out = plan_tick(parts, {}, 2)
    assert out["mark_failed"] == [0]
    assert out["complete"] is True


def test_done_and_failed_parts_never_revert():
    # A done part whose run later shows "failed" (or vice versa) stays put:
    # only running parts are inspected at all.
    parts = [
        _part(0, state="done", run_uid="r0"),
        _part(1, state="failed", run_uid="r1"),
    ]
    out = plan_tick(parts, {"r0": "failed", "r1": "awaiting_input"}, 2)
    assert out["mark_done"] == [] and out["mark_failed"] == []
    assert out["complete"] is True


def test_globals_gated_until_all_areas_terminal():
    parts = [
        _part(0, state="running", run_uid="r0"),
        _part(1, kind="global"),
    ]
    out = plan_tick(parts, {"r0": "running"}, 4)
    assert out["dispatch"] == []  # area still in flight — global must wait

    # The area finishing THIS tick unlocks the global in the same tick.
    out = plan_tick(parts, {"r0": "awaiting_input"}, 4)
    assert out["mark_done"] == [0]
    assert out["dispatch"] == [1]


def test_failed_areas_still_unlock_globals():
    parts = [_part(0, state="failed"), _part(1, kind="global")]
    out = plan_tick(parts, {}, 2)
    assert out["dispatch"] == [1]


def test_feature_parts_dispatch_like_areas_and_gate_globals():
    # Feature parts are non-global: they dispatch immediately and their
    # findings must land before the global sweeps' escalation digests run.
    parts = [_part(0, kind="feature"), _part(1, kind="global")]
    out = plan_tick(parts, {}, 4)
    assert out["dispatch"] == [0]  # the global waits for the feature part


def test_capacity_zero_dispatches_nothing():
    parts = [
        _part(0, state="running", run_uid="r0"),
        _part(1, state="running", run_uid="r1"),
        _part(2),
    ]
    out = plan_tick(parts, {"r0": "running", "r1": "queued"}, 2)
    assert out["dispatch"] == []


def test_complete_when_all_parts_terminal_including_failures():
    parts = [_part(0, state="done"), _part(1, state="failed")]
    out = plan_tick(parts, {}, 2)
    assert out["complete"] is True


def test_not_complete_while_pending_or_in_flight_remain():
    assert plan_tick([_part(0)], {}, 0)["complete"] is False
    parts = [_part(0, state="running", run_uid="r0")]
    assert plan_tick(parts, {"r0": "running"}, 2)["complete"] is False


def test_complete_after_last_running_part_terminates_this_tick():
    parts = [_part(0, state="running", run_uid="r0"), _part(1, state="done")]
    out = plan_tick(parts, {"r0": "ended"}, 2)
    assert out["mark_done"] == [0]
    assert out["complete"] is True
