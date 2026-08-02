"""The admission gate in `lifecycle.trigger_run`.

Bounds runs per PROVIDER, cross-process, by refusing before any Run row exists.
The no-row property is what keeps it from deadlocking: `active_run_count`
counts queued/running/paused_quota, so a row parked awaiting capacity would
consume the headroom it is waiting for and never recover.
"""

from types import SimpleNamespace

import pytest

from domains.runs.services import lifecycle
from domains.runs.services.lifecycle import (
    CapacityExceededError,
    CapacityMode,
    LifecycleError,
    _assert_capacity,
)


def _provider(uid="p-1", ceiling=5, label="Claude Sub"):
    return SimpleNamespace(
        uid=uid, max_concurrent_runs=ceiling, kind="claude_subscription", label=label
    )


def _with_active(monkeypatch, count):
    async def _count(_uid):
        return count

    monkeypatch.setattr(
        "domains.llm_providers.services.capacity.active_run_count", _count
    )


def _capture_audit(monkeypatch):
    written = []

    async def _write(**kwargs):
        written.append(kwargs)

    monkeypatch.setattr(lifecycle, "write_audit", _write)
    return written


@pytest.mark.asyncio
async def test_under_the_ceiling_admits_without_flagging(monkeypatch):
    _with_active(monkeypatch, 2)
    over = await _assert_capacity(
        _provider(), mode=CapacityMode.ENFORCE, playbook="implement", repository_uid="r1"
    )
    assert over is False


@pytest.mark.asyncio
async def test_at_the_ceiling_refuses_under_enforce(monkeypatch):
    _with_active(monkeypatch, 5)
    _capture_audit(monkeypatch)
    with pytest.raises(CapacityExceededError) as exc:
        await _assert_capacity(
            _provider(),
            mode=CapacityMode.ENFORCE,
            playbook="implement",
            repository_uid="r1",
        )
    assert "5/5" in str(exc.value)
    assert "Claude Sub" in str(exc.value)


@pytest.mark.asyncio
async def test_refusal_is_a_LifecycleError_so_routes_already_map_it_to_409(monkeypatch):
    """No API route needs to learn about capacity: every boundary already
    catches LifecycleError and raises 409."""
    _with_active(monkeypatch, 5)
    _capture_audit(monkeypatch)
    with pytest.raises(LifecycleError):
        await _assert_capacity(
            _provider(), mode=CapacityMode.ENFORCE, playbook="ask", repository_uid="r1"
        )


@pytest.mark.asyncio
async def test_refusal_is_audited_as_the_ops_signal(monkeypatch):
    _with_active(monkeypatch, 7)
    written = _capture_audit(monkeypatch)
    with pytest.raises(CapacityExceededError):
        await _assert_capacity(
            _provider(),
            mode=CapacityMode.ENFORCE,
            playbook="implement",
            repository_uid="r1",
        )
    assert len(written) == 1
    assert written[0]["kind"] == "run.capacity_refused"
    assert written[0]["payload"]["active"] == 7
    assert written[0]["payload"]["ceiling"] == 5
    assert written[0]["payload"]["playbook"] == "implement"


@pytest.mark.asyncio
async def test_best_effort_admits_over_the_ceiling_and_reports_it(monkeypatch):
    """The verification/skeptic path. Its caller turns any LifecycleError into
    a PERMANENT verification_status='failed', so refusing it would delete the
    pass outright with no retry driver."""
    _with_active(monkeypatch, 9)
    written = _capture_audit(monkeypatch)
    over = await _assert_capacity(
        _provider(),
        mode=CapacityMode.BEST_EFFORT,
        playbook="verify",
        repository_uid="r1",
    )
    assert over is True, "caller must be told, so it can stamp usage['over_ceiling']"
    assert written == [], "an admitted run is not a refusal — do not audit it as one"


@pytest.mark.asyncio
async def test_best_effort_under_the_ceiling_is_not_flagged(monkeypatch):
    _with_active(monkeypatch, 1)
    over = await _assert_capacity(
        _provider(),
        mode=CapacityMode.BEST_EFFORT,
        playbook="verify",
        repository_uid="r1",
    )
    assert over is False
