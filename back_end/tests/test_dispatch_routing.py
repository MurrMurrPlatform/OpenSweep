"""trigger_run's dispatch launcher must not orphan the pipeline in a Celery
worker.

Regression for "campaign runs stuck QUEUED / preparing sandbox — cloning": a
worker-context dispatch used a fire-and-forget `asyncio.create_task`, which the
tick's short-lived `asyncio.run` loop cancelled mid-clone. The fix hands worker
dispatches off to the per-run `dispatch_run` Celery task (its own loop);
backend (long-lived ASGI loop) dispatches still run inline.
"""

import asyncio

import pytest

from domains.runs.services import lifecycle
from infrastructure import process_role


@pytest.fixture
def restore_role():
    original = process_role.get_role()
    yield
    process_role.set_role(original)


class _Run:
    def __init__(self, uid: str) -> None:
        self.uid = uid


async def test_worker_dispatch_hands_off_to_celery(monkeypatch, restore_role):
    """WORKER role → enqueue dispatch_run, never run the pipeline in-loop."""
    process_role.set_role(process_role.WORKER)
    enqueued: list[str] = []
    monkeypatch.setattr(
        "domains.runs.tasks.dispatch_runs.dispatch_run.apply_async",
        lambda args=None, **kw: enqueued.append((args[0], kw)),
    )
    ran: list[bool] = []

    def make_pipeline():
        ran.append(True)  # building/awaiting the pipeline here would be the bug

        async def _noop():
            return None

        return _noop()

    run = _Run("run-worker")
    out = await lifecycle._launch_dispatch(run, make_pipeline, wait_for_completion=False)

    assert out is run
    assert ran == []  # pipeline was NOT started in the tick's loop
    assert len(enqueued) == 1
    uid, kwargs = enqueued[0]
    assert uid == "run-worker"
    # Policy-derived limits ride along, so the task ceiling sits above the
    # wall ceiling the executor enforces rather than a flat 3900s.
    assert kwargs["time_limit"] > kwargs["soft_time_limit"]


async def test_backend_dispatch_runs_inline(monkeypatch, restore_role):
    """BACKEND role → in-process background task runs; no celery hand-off."""
    process_role.set_role(process_role.BACKEND)
    enqueued: list[str] = []
    monkeypatch.setattr(
        "domains.runs.tasks.dispatch_runs.dispatch_run.apply_async",
        lambda args=None, **kw: enqueued.append((args[0], kw)),
    )
    ran = asyncio.Event()

    def make_pipeline():
        async def _go():
            ran.set()

        return _go()

    run = _Run("run-backend")
    out = await lifecycle._launch_dispatch(run, make_pipeline, wait_for_completion=False)

    assert out is run
    await asyncio.wait_for(ran.wait(), timeout=1)  # background task actually ran
    assert enqueued == []  # did NOT hand off to celery


async def test_wait_for_completion_runs_inline_even_in_worker(monkeypatch, restore_role):
    """wait_for_completion awaits the pipeline inline regardless of role."""
    process_role.set_role(process_role.WORKER)
    enqueued: list[str] = []
    monkeypatch.setattr(
        "domains.runs.tasks.dispatch_runs.dispatch_run.apply_async",
        lambda args=None, **kw: enqueued.append((args[0], kw)),
    )
    ran: list[bool] = []
    reloaded = object()

    def make_pipeline():
        async def _go():
            ran.append(True)

        return _go()

    class _Nodes:
        async def get(self, uid=None):
            return reloaded

    monkeypatch.setattr(lifecycle.Run, "nodes", _Nodes())

    run = _Run("run-wait")
    out = await lifecycle._launch_dispatch(run, make_pipeline, wait_for_completion=True)

    assert ran == [True]  # ran inline
    assert out is reloaded  # returns the reloaded row
    assert enqueued == []  # no celery hand-off when awaiting inline
