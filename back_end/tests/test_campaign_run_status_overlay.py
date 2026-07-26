"""to_dto_live overlays each part with its Run's LIVE status.

The planner's `state` is a 4-value, forward-only, once-a-minute snapshot;
these tests pin the cases where it disagrees with the Run and assert the
DTO carries the truth alongside it rather than instead of it.
"""

from unittest.mock import MagicMock, patch

import pytest

from domains.campaigns.models import Campaign
from domains.campaigns.services import campaign_service


def _campaign(parts):
    c = MagicMock(spec=Campaign)
    c.uid = "c-1"
    c.repository_uid = "repo-1"
    c.title = "Campaign"
    c.status = "running"
    c.template = "rotation"
    c.kind = "subsystem"
    c.selection = "all"
    c.coverage_keys = []
    c.parent_uid = ""
    c.child_uids = []
    c.effort = "normal"
    c.lens_keys = []
    c.k = 3
    c.area_prefix = ""
    c.parts = parts
    c.max_parallel = 5
    c.created_by = ""
    c.trigger_provenance = ""
    c.summary = {}
    c.plan_summary = {}
    c.events = []
    c.created_at = None
    c.updated_at = None
    return c


def _part(idx, *, state, run_uid=""):
    return {"idx": idx, "kind": "area", "title": f"p{idx}", "state": state,
            "run_uid": run_uid, "scope_paths": [], "doc_uids": [],
            "lens_keys": [], "file_count": 0, "area_keys": []}


async def _live(campaign, statuses):
    with patch.object(
        campaign_service, "_run_statuses", return_value=statuses
    ) as q:
        dto = await campaign_service.to_dto_live(campaign)
    return dto, q


@pytest.mark.asyncio
async def test_awaiting_input_part_reports_the_run_not_the_planner_state():
    """The planner calls awaiting_input 'done' (tick._RUN_DONE) because the
    part is finished from ITS point of view. The run is not — the composer
    is live and the user is expected to reply."""
    c = _campaign([_part(0, state="done", run_uid="r0")])
    dto, _ = await _live(c, {"r0": "awaiting_input"})
    assert dto.parts[0]["state"] == "done"
    assert dto.parts[0]["run_status"] == "awaiting_input"


@pytest.mark.asyncio
async def test_sticky_done_part_still_shows_a_restarted_run():
    """Replying to an awaiting_input run puts it back to `running`, but part
    state never reverts (tick.plan_tick) — the overlay is the only way the
    UI can tell."""
    c = _campaign([_part(0, state="done", run_uid="r0")])
    dto, _ = await _live(c, {"r0": "running"})
    assert dto.parts[0]["run_status"] == "running"


@pytest.mark.asyncio
async def test_cancelled_campaign_parts_still_track_their_runs():
    """Cancel leaves in-flight child runs alive and freezes the parts (the
    tick only persists for status='running'), so without the overlay a
    cancelled campaign shows `running` forever."""
    c = _campaign([_part(0, state="running", run_uid="r0")])
    c.status = "cancelled"
    dto, _ = await _live(c, {"r0": "ended"})
    assert dto.parts[0]["run_status"] == "ended"


@pytest.mark.asyncio
async def test_paused_quota_is_visible_instead_of_collapsing_to_running():
    c = _campaign([_part(0, state="running", run_uid="r0")])
    dto, _ = await _live(c, {"r0": "paused_quota"})
    assert dto.parts[0]["run_status"] == "paused_quota"


@pytest.mark.asyncio
async def test_undispatched_part_gets_no_run_status_key():
    """A pending part has no run yet — absent, not empty, so the UI falls
    back to `state` rather than rendering a blank badge."""
    c = _campaign([_part(0, state="pending")])
    dto, q = await _live(c, {})
    assert "run_status" not in dto.parts[0]
    q.assert_awaited_once_with([])


@pytest.mark.asyncio
async def test_missing_run_leaves_state_alone():
    """The run row is gone (deleted). Don't invent a status."""
    c = _campaign([_part(0, state="running", run_uid="r0")])
    dto, _ = await _live(c, {})
    assert "run_status" not in dto.parts[0]
    assert dto.parts[0]["state"] == "running"


@pytest.mark.asyncio
async def test_all_run_uids_fetched_in_one_query():
    """One query for the whole campaign, not one per part."""
    c = _campaign([_part(i, state="running", run_uid=f"r{i}") for i in range(4)])
    dto, q = await _live(c, {f"r{i}": "running" for i in range(4)})
    q.assert_awaited_once_with(["r0", "r1", "r2", "r3"])
    assert all(p["run_status"] == "running" for p in dto.parts)
