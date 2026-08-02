"""A capacity refusal DEFERS a campaign part; it never fails it.

`plan_tick` clamps dispatch to the headroom it read at the top of the tick, but
a manual dispatch or a second campaign can take the slot before `dispatch_part`
runs. The refusal that follows is transient — and `part["state"] = "failed"` is
permanent (`plan_tick` never reverts done/failed parts), so routing capacity
through the generic `except Exception` branch would let ordinary provider
pressure silently destroy campaign work.
"""

from types import SimpleNamespace

import pytest

from domains.campaigns.services import tick
from domains.runs.services.lifecycle import CapacityExceededError


def _campaign():
    return SimpleNamespace(
        uid="c1",
        repository_uid="repo1",
        status="running",
        max_parallel=5,
        parts=[
            {"idx": 0, "kind": "area", "title": "A", "run_uid": "", "state": "pending"},
            {"idx": 1, "kind": "area", "title": "B", "run_uid": "", "state": "pending"},
        ],
        events=[],
        updated_at=None,
    )


class _Saved:
    """Stands in for the refetch-before-save Campaign.nodes lookup."""

    def __init__(self, campaign):
        self.campaign = campaign

    async def get_or_none(self, **_):
        return self.campaign


def _patch_common(monkeypatch, campaign):
    from domains.campaigns.models import Campaign
    from domains.runs.models import Run

    async def _save(self=None):
        return None

    campaign.save = _save
    monkeypatch.setattr(Campaign, "nodes", _Saved(campaign), raising=False)
    monkeypatch.setattr(
        Run, "nodes", SimpleNamespace(get_or_none=lambda **_: None), raising=False
    )
    monkeypatch.setattr(tick, "_provider_headroom", lambda _c: _none())


async def _none():
    return None


@pytest.mark.asyncio
async def test_capacity_refusal_leaves_the_part_pending(monkeypatch):
    campaign = _campaign()
    _patch_common(monkeypatch, campaign)

    async def _refuse(_c, _part):
        raise CapacityExceededError("provider at ceiling (5/5)")

    monkeypatch.setattr(
        "domains.campaigns.services.part_dispatch.dispatch_part", _refuse
    )

    dispatched, _ = await tick._tick_one(campaign)

    assert dispatched == 0
    states = [p["state"] for p in campaign.parts]
    assert states == ["pending", "pending"], (
        "a deferred part must stay pending — `failed` is permanent and would "
        "lose the work"
    )
    kinds = [e["type"] for e in campaign.events]
    assert "part_deferred" in kinds
    assert "part_dispatch_failed" not in kinds


@pytest.mark.asyncio
async def test_capacity_refusal_stops_the_tick_rather_than_grinding(monkeypatch):
    """The break matters: once the provider is full, every remaining part in
    this tick would refuse too. Continuing would emit one deferral event per
    pending part on every 60s beat."""
    campaign = _campaign()
    _patch_common(monkeypatch, campaign)
    calls = []

    async def _refuse(_c, part):
        calls.append(part["idx"])
        raise CapacityExceededError("provider at ceiling (5/5)")

    monkeypatch.setattr(
        "domains.campaigns.services.part_dispatch.dispatch_part", _refuse
    )

    await tick._tick_one(campaign)

    assert calls == [0], "should stop after the first refusal, not try every part"


@pytest.mark.asyncio
async def test_a_real_dispatch_error_still_fails_the_part(monkeypatch):
    """The generic branch must keep its behaviour — this test is what stops the
    new `except CapacityExceededError` from being widened later."""
    campaign = _campaign()
    _patch_common(monkeypatch, campaign)

    async def _boom(_c, _part):
        raise ValueError("compose blew up")

    monkeypatch.setattr(
        "domains.campaigns.services.part_dispatch.dispatch_part", _boom
    )

    await tick._tick_one(campaign)

    assert campaign.parts[0]["state"] == "failed"
    assert "part_dispatch_failed" in [e["type"] for e in campaign.events]
