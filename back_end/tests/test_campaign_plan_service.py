"""Kind-aware create() translation + preview_plan — plan seams stubbed.

create() reads req.kind/coverage_keys/selection directly; an empty kind
translates the legacy template (rotation→subsystem/rotation, focused→
subsystem+first lens, full→batch). preview_plan runs the same planning path
without persisting.
"""

from types import SimpleNamespace

import pytest

from domains.campaigns.models import Campaign
from domains.campaigns.schemas import CreateCampaignRequest
from domains.campaigns.services import batch, campaign_service


@pytest.fixture
def create_seams(monkeypatch):
    """_plan_parts + save + record_event + write_audit stubbed; create() runs
    real. Captures the kind/coverage/selection/lens_keys it planned with."""
    captured = {}

    async def fake_plan_parts(
        repository_uid, *, kind, coverage_keys, selection, lens_keys, k
    ):
        captured.update(
            kind=kind,
            coverage_keys=coverage_keys,
            selection=selection,
            lens_keys=lens_keys,
            k=k,
        )
        return ([{"idx": 0, "kind": "area", "title": "a"}], "", "area-map", {})

    async def fake_save(self):
        return self

    async def fake_record_event(c, type, **payload):
        return None

    async def fake_audit(**_kw):
        return None

    monkeypatch.setattr(campaign_service, "_plan_parts", fake_plan_parts)
    monkeypatch.setattr(Campaign, "save", fake_save)
    monkeypatch.setattr(campaign_service, "record_event", fake_record_event)
    monkeypatch.setattr(campaign_service, "write_audit", fake_audit)
    return captured


async def test_create_uses_explicit_kind_verbatim(create_seams):
    req = CreateCampaignRequest(
        kind="feature", selection="stale", coverage_keys=["features/checkout"]
    )
    c = await campaign_service.create("repo1", req)
    assert c.kind == "feature"
    assert c.selection == "stale"
    assert c.coverage_keys == ["features/checkout"]
    assert create_seams["kind"] == "feature"
    assert create_seams["selection"] == "stale"


async def test_legacy_rotation_translates_to_subsystem_rotation(create_seams):
    c = await campaign_service.create("repo1", CreateCampaignRequest(template="rotation"))
    assert c.kind == "subsystem"
    assert c.selection == "rotation"
    assert create_seams["kind"] == "subsystem"
    assert create_seams["selection"] == "rotation"


async def test_legacy_focused_uses_only_the_first_lens(create_seams):
    c = await campaign_service.create(
        "repo1",
        CreateCampaignRequest(template="focused", lens_keys=["security", "bugs"]),
    )
    assert c.kind == "subsystem"
    assert create_seams["lens_keys"] == ["security"]


async def test_legacy_area_prefix_becomes_coverage_keys(create_seams):
    c = await campaign_service.create(
        "repo1", CreateCampaignRequest(template="rotation", area_prefix="backend")
    )
    assert c.coverage_keys == ["backend"]
    assert create_seams["coverage_keys"] == ["backend"]


async def test_legacy_full_template_delegates_to_batch(monkeypatch):
    called = {}

    async def fake_create_batch(repository_uid, req, *, created_by="", trigger_provenance=""):
        called["repo"] = repository_uid
        called["kind"] = "batch"
        return SimpleNamespace(uid="batch1", kind="batch")

    monkeypatch.setattr(batch, "create_batch", fake_create_batch)

    out = await campaign_service.create("repo1", CreateCampaignRequest(template="full"))
    assert out.uid == "batch1"
    assert called["kind"] == "batch"


async def test_explicit_batch_kind_delegates_to_batch(monkeypatch):
    called = {}

    async def fake_create_batch(repository_uid, req, *, created_by="", trigger_provenance=""):
        called["yes"] = True
        return SimpleNamespace(uid="batch1", kind="batch")

    monkeypatch.setattr(batch, "create_batch", fake_create_batch)

    out = await campaign_service.create("repo1", CreateCampaignRequest(kind="batch"))
    assert out.uid == "batch1"
    assert called["yes"] is True


# ── preview_plan ─────────────────────────────────────────────────────────────


@pytest.fixture
def preview_seams(monkeypatch):
    """_plan_parts stubbed to a known plan so preview_plan's projection is
    asserted without a DB."""

    async def fake_plan_parts(
        repository_uid, *, kind, coverage_keys, selection, lens_keys, k
    ):
        parts = [
            {"idx": 0, "kind": "area", "title": "Backend", "scope_paths": ["be"],
             "area_keys": ["backend"], "file_count": 40},
            {"idx": 1, "kind": "area", "title": "Frontend", "scope_paths": ["fe"],
             "area_keys": ["frontend"], "file_count": 20},
        ]
        summary = {
            "total_runs": 2,
            "by_kind": {"area": 2, "feature": 0, "global": 0},
            "oversized": ["Frontend"],
        }
        return parts, "", "area-map", summary

    monkeypatch.setattr(campaign_service, "_plan_parts", fake_plan_parts)


async def test_preview_plan_reports_run_shape_without_persisting(preview_seams):
    out = await campaign_service.preview_plan(
        "repo1", CreateCampaignRequest(kind="subsystem")
    )
    assert out["total_runs"] == 2
    assert out["by_kind"] == {"area": 2, "feature": 0, "global": 0}
    assert out["source"] == "area-map"
    assert out["oversized"] == ["Frontend"]
    assert [a["title"] for a in out["areas"]] == ["Backend", "Frontend"]
    assert out["areas"][0]["area_keys"] == ["backend"]
    # total_runs always equals the number of planned parts.
    assert out["total_runs"] == len(out["areas"])


async def test_preview_plan_batch_totals_every_child_kind(monkeypatch):
    async def fake_plan_parts(
        repository_uid, *, kind, coverage_keys, selection, lens_keys, k
    ):
        by_kind = {
            "subsystem": {"area": 3, "feature": 0, "global": 0},
            "feature": {"area": 0, "feature": 2, "global": 0},
            "global": {"area": 0, "feature": 0, "global": 1},
        }[kind]
        return [], "", "area-map", {"by_kind": by_kind, "oversized": []}

    monkeypatch.setattr(campaign_service, "_plan_parts", fake_plan_parts)

    out = await campaign_service.preview_plan(
        "repo1", CreateCampaignRequest(kind="batch")
    )
    assert out["source"] == "batch"
    assert out["by_kind"] == {"area": 3, "feature": 2, "global": 1}
    assert out["total_runs"] == 6
