"""In-process dispatch fan-out bound.

Bounds pipelines hosted by ONE backend process — a local-resource backstop,
distinct from the per-provider concurrency ceiling. The subtle requirement is
the per-loop rebuild: a Semaphore's waiters belong to the loop that created it.
"""

import asyncio

import pytest

from domains.runs.services import lifecycle


@pytest.fixture(autouse=True)
def _reset_slots():
    lifecycle._DISPATCH_SLOTS = None
    lifecycle._DISPATCH_SLOTS_LOOP = None
    yield
    lifecycle._DISPATCH_SLOTS = None
    lifecycle._DISPATCH_SLOTS_LOOP = None


@pytest.mark.asyncio
async def test_same_loop_reuses_one_semaphore():
    assert lifecycle._dispatch_slots() is lifecycle._dispatch_slots()


def test_a_new_event_loop_gets_a_fresh_semaphore():
    """The regression guard. A module-level Semaphore shared across loops parks
    waiters on a dead loop and hangs forever — and the test suite, celery's
    asyncio.run, and the worker each create their own loop."""
    lifecycle._DISPATCH_SLOTS = None
    lifecycle._DISPATCH_SLOTS_LOOP = None

    first = asyncio.run(_grab())
    second = asyncio.run(_grab())
    assert first is not second, "semaphore must be rebuilt when the loop changes"


async def _grab():
    return lifecycle._dispatch_slots()


@pytest.mark.asyncio
async def test_the_bound_actually_serialises_beyond_capacity(monkeypatch):
    monkeypatch.setattr(
        lifecycle.settings, "OPENSWEEP_BACKEND_MAX_INFLIGHT_DISPATCHES", 2
    )

    async def _no_run(**_):
        return None

    monkeypatch.setattr(lifecycle.Run, "nodes", type("N", (), {"get_or_none": staticmethod(_no_run)}))

    peak = 0
    live = 0
    release = asyncio.Event()

    async def _pipeline():
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        await release.wait()
        live -= 1

    tasks = [
        asyncio.create_task(lifecycle._bounded_pipeline(f"r{i}", _pipeline))
        for i in range(5)
    ]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*tasks)

    assert peak <= 2, f"expected at most 2 concurrent pipelines, saw {peak}"


@pytest.mark.asyncio
async def test_a_zero_or_negative_setting_still_yields_a_usable_bound(monkeypatch):
    """Misconfiguration must not wedge every dispatch — clamp to 1 rather than
    constructing Semaphore(0), which never admits anyone."""
    monkeypatch.setattr(
        lifecycle.settings, "OPENSWEEP_BACKEND_MAX_INFLIGHT_DISPATCHES", 0
    )
    slots = lifecycle._dispatch_slots()
    async with slots:
        pass
