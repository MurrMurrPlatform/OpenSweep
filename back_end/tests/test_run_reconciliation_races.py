"""Reconciler write races and queue-time reaping.

Two defects this guards:

1. `_fail_run` used to write the caller's SNAPSHOT of the Run back with
   neomodel's full-property `save()`. A run that completed between the
   reconciler's fetch and the save was clobbered back to "failed" — and its
   playbook completion hook fired a second time.
2. `queued` counts as repairable, but a queued run has no executor activity:
   `last_activity` collapses to creation time, so the liveness timeout failed
   backlogged runs with a misleading "the dispatching process likely
   restarted or crashed".
"""

from datetime import datetime, timedelta, timezone

import pytest

from domains.runs.services import run_reconciliation as rr

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


class _FakeRun:
    def __init__(self, uid, status, *, created_at=None, usage=None):
        self.uid = uid
        self.status = status
        self.created_at = created_at or NOW - timedelta(seconds=5000)
        self.started_at = self.created_at
        self.last_activity_at = self.created_at
        self.completed_at = None
        self.duration_ms = 0
        self.updated_at = self.created_at
        self.error = ""
        self.usage = usage or {}
        self.run_policy_uid = ""
        self.saves = 0

    async def save(self):
        self.saves += 1


def _patch_side_effects(monkeypatch):
    """Silence the audit / transcript / hook side effects of a repair."""
    monkeypatch.setattr(rr, "write_audit", _noop_async)
    monkeypatch.setattr(rr, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(rr.playbook_registry, "on_turn_complete", _noop_async)


async def _noop_async(*_a, **_kw):
    return None


def _patch_lookup(monkeypatch, rows: dict):
    """Point `Run.nodes.get_or_none(uid=...)` at an in-memory table."""

    class _Nodes:
        async def get_or_none(self, uid):
            return rows.get(uid)

    class _Run:
        nodes = _Nodes()

    monkeypatch.setattr(rr, "Run", _Run)


# --- _fail_run refetch guard ------------------------------------------------


async def test_fail_run_does_not_clobber_a_run_that_finished_meanwhile(monkeypatch):
    # The reconciler's snapshot still says "running"; the DB row has since
    # moved to awaiting_input because the adapter finished normally.
    snapshot = _FakeRun("run-1", "running")
    live = _FakeRun("run-1", "awaiting_input")
    _patch_lookup(monkeypatch, {"run-1": live})
    _patch_side_effects(monkeypatch)

    hooks: list[str] = []

    async def _hook(run):
        hooks.append(run.uid)

    monkeypatch.setattr(rr.playbook_registry, "on_turn_complete", _hook)

    repaired = await rr._fail_run(
        snapshot,
        now=NOW,
        error="stale",
        audit_kind="run.reconciled_failed",
        usage_flag="reconciled_stale",
        payload={},
    )

    assert repaired is False
    # The completed run keeps its status and is never re-saved, and the
    # completion hook does not fire a second time.
    assert live.status == "awaiting_input"
    assert live.saves == 0
    assert hooks == []


async def test_fail_run_repairs_a_still_running_row_and_updates_the_snapshot(monkeypatch):
    snapshot = _FakeRun("run-2", "running")
    live = _FakeRun("run-2", "running")
    _patch_lookup(monkeypatch, {"run-2": live})
    _patch_side_effects(monkeypatch)

    repaired = await rr._fail_run(
        snapshot,
        now=NOW,
        error="no activity",
        audit_kind="run.reconciled_failed",
        usage_flag="reconciled_stale",
        payload={},
    )

    assert repaired is True
    assert live.status == "failed" and live.saves == 1
    assert live.usage["reconciled_stale"] is True
    # reconcile_runs documents that it repairs IN PLACE — callers re-filter
    # the list they passed in, so the snapshot must carry the outcome too.
    assert snapshot.status == "failed"
    assert snapshot.error == "no activity"


async def test_fail_run_tolerates_a_deleted_row(monkeypatch):
    _patch_lookup(monkeypatch, {})
    _patch_side_effects(monkeypatch)
    assert (
        await rr._fail_run(
            _FakeRun("gone", "running"),
            now=NOW,
            error="x",
            audit_kind="k",
            usage_flag="f",
            payload={},
        )
        is False
    )


# --- queued runs are not reaped for waiting ---------------------------------


async def test_backlogged_queued_run_is_not_failed_for_queue_time(monkeypatch):
    # Queued 5000s ago (well past the 900s liveness timeout) but dispatch has
    # not started: it is waiting behind other runs on a busy worker.
    queued = _FakeRun("run-q", "queued")
    monkeypatch.setattr(rr, "dispatch_started", lambda run: False)
    _patch_lookup(monkeypatch, {"run-q": queued})
    _patch_side_effects(monkeypatch)

    assert await rr.reconcile_runs([queued]) == 0
    assert queued.status == "queued" and queued.saves == 0


async def test_queued_run_is_failed_once_dispatch_started_and_went_silent(monkeypatch):
    # Same row, but the transcript exists — dispatch DID start and then went
    # quiet past the liveness timeout, which is a genuine crash signal.
    queued = _FakeRun("run-q2", "queued")
    monkeypatch.setattr(rr, "dispatch_started", lambda run: True)
    monkeypatch.setattr(rr, "last_activity", lambda run: NOW - timedelta(seconds=5000))
    _patch_lookup(monkeypatch, {"run-q2": queued})
    _patch_side_effects(monkeypatch)

    assert await rr.reconcile_runs([queued]) == 1
    assert queued.status == "failed"


async def test_dispatch_started_reads_the_transcript_file(monkeypatch):
    class _Missing:
        def stat(self):
            raise OSError("no such file")

    class _Present:
        def stat(self):
            return None

    monkeypatch.setattr(rr, "events_path", lambda uid: _Missing())
    assert rr.dispatch_started(_FakeRun("a", "queued")) is False
    monkeypatch.setattr(rr, "events_path", lambda uid: _Present())
    assert rr.dispatch_started(_FakeRun("a", "queued")) is True
