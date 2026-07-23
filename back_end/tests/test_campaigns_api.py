"""Campaign API surface — plan-preview endpoint + kind-based create route.

Tests the new POST /repositories/{uid}/campaign-plan-preview endpoint:
- Returns total_runs from preview_plan without persisting a campaign.
- create with {"kind":"global"} yields parts all of part-kind global.

DB-free: all service and auth seams monkeypatched.
"""

from types import SimpleNamespace

import pytest

import api.v1.campaigns as campaigns_mod
from domains.campaigns.schemas import CreateCampaignRequest
from domains.campaigns.services import campaign_service
from domains.users.schemas import UserDTO


def _user() -> UserDTO:
    return UserDTO(
        uid="u1",
        email="u@example.test",
        display_name="U",
        role="admin",
        org_uid="org-a",
        org_role="owner",
        is_platform_admin=False,
    )


@pytest.fixture(autouse=True)
def _tenancy_noop(monkeypatch):
    async def allow(repository_uid, org_uid):
        return None

    monkeypatch.setattr(campaigns_mod, "require_repo_in_org", allow)


# ── Plan-preview endpoint ────────────────────────────────────────────────────


async def test_plan_preview_endpoint_returns_total_runs_without_persisting(monkeypatch):
    """POST /repositories/{uid}/campaign-plan-preview returns preview_plan's
    dict and does NOT call campaign_service.create."""

    preview_result = {
        "total_runs": 4,
        "by_kind": {"area": 0, "feature": 0, "global": 4},
        "areas": [],
        "uncovered_files": 0,
        "oversized": [],
        "degraded": "",
        "source": "global",
    }

    async def fake_preview_plan(repository_uid, req):
        assert repository_uid == "repo-x"
        assert req.kind == "global"
        return preview_result

    create_called = []

    async def must_not_create(*args, **kwargs):  # pragma: no cover
        create_called.append(True)
        raise AssertionError("create was called — preview must not persist")

    monkeypatch.setattr(campaign_service, "preview_plan", fake_preview_plan)
    monkeypatch.setattr(campaign_service, "create", must_not_create)

    req = CreateCampaignRequest(kind="global")
    result = await campaigns_mod.preview_campaign_plan("repo-x", req, user=_user())

    assert result["total_runs"] == 4
    assert result["source"] == "global"
    assert create_called == [], "preview endpoint must not call create()"


async def test_plan_preview_total_runs_matches_global_create_part_count(monkeypatch):
    """Preview for kind=global and create for kind=global agree on total_runs.

    Both delegate to the same _plan_parts logic; we stub _plan_parts once and
    assert that preview_plan's total_runs equals what create() would plan."""
    global_parts = [
        {"idx": 0, "kind": "global", "title": "Security sweep", "scope_paths": [],
         "area_keys": [], "lens_keys": ["security"], "file_count": None},
        {"idx": 1, "kind": "global", "title": "Bugs sweep", "scope_paths": [],
         "area_keys": [], "lens_keys": ["bugs"], "file_count": None},
    ]
    plan_summary = {
        "total_runs": 2,
        "by_kind": {"area": 0, "feature": 0, "global": 2},
        "source": "global",
        "oversized": [],
        "degraded": "",
        "map_areas": 0,
        "leaves": 0,
        "groupings": 0,
        "features": 0,
        "feature_groupings": 0,
        "ignored": 0,
        "area_parts": 0,
        "bundled_leaves": 0,
        "feature_parts": 0,
        "global_parts": 2,
        "coverage_keys": [],
        "selection": "all",
    }

    async def fake_plan_parts(repository_uid, *, kind, coverage_keys, selection, lens_keys, k):
        return global_parts, "", "global", plan_summary

    monkeypatch.setattr(campaign_service, "_plan_parts", fake_plan_parts)

    # Preview path — no persistence.
    req = CreateCampaignRequest(kind="global")
    preview = await campaign_service.preview_plan("repo-x", req)
    assert preview["total_runs"] == 2
    assert preview["by_kind"]["global"] == 2

    # Create path — stub save/record_event/write_audit so no DB needed.
    from domains.campaigns.models import Campaign

    async def fake_save(self):
        return self

    async def fake_record_event(c, event_type, **payload):
        return None

    async def fake_write_audit(**_kw):
        return None

    monkeypatch.setattr(Campaign, "save", fake_save)
    monkeypatch.setattr(campaign_service, "record_event", fake_record_event)
    monkeypatch.setattr(campaign_service, "write_audit", fake_write_audit)

    created = await campaign_service.create("repo-x", req)
    # All parts must be of part-kind "global".
    assert all(p["kind"] == "global" for p in created.parts), (
        "create with kind=global must yield only global parts"
    )
    # Part count matches preview's total_runs.
    assert len(created.parts) == preview["total_runs"]


# ── Route registration ────────────────────────────────────────────────────────


def test_campaign_plan_preview_route_is_registered():
    from app import app

    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/v1/repositories/{repository_uid}/campaign-plan-preview" in paths


def test_campaign_plan_preview_operation_id():
    from app import app

    schema = app.openapi()
    ops = set()
    for methods in schema.get("paths", {}).values():
        for op in methods.values():
            if isinstance(op, dict) and op.get("operationId"):
                ops.add(op["operationId"])
    assert "opensweep_campaign_plan_preview" in ops
