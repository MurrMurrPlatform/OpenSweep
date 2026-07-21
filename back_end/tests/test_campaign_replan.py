"""Launch-time replan + preview endpoint helper — plan seams monkeypatched.

launch() must run against today's partition, not the creation-time snapshot:
changed parts get replaced (with a "replanned" event), an unchanged plan stays
silent, and a degraded/failed recompute keeps the original parts
("replan_skipped"). preview_areas computes the partition without persisting.
"""

from types import SimpleNamespace

import pytest

from domains.campaigns.models import Campaign
from domains.campaigns.services import campaign_service


def _part(idx):
    return {
        "idx": idx,
        "kind": "area",
        "title": f"p{idx}",
        "scope_paths": [f"p{idx}"],
        "doc_uids": [],
        "lens_keys": ["bugs"],
        "run_uid": "",
        "state": "pending",
        "file_count": 10,
        "area_key": "",
    }


def _campaign(**overrides):
    fields = {
        "uid": "c1",
        "repository_uid": "repo1",
        "title": "t",
        "status": "planning",
        "template": "full",
        "effort": "",
        "lens_keys": ["bugs"],
        "k": 3,
        "parts": [_part(0)],
        "max_parallel": 2,
    }
    fields.update(overrides)
    return Campaign(**fields)


@pytest.fixture
def seams(monkeypatch):
    """launch() with the DB seams stubbed: get → in-memory campaign,
    save → counted no-op, record_event → captured list."""
    state = SimpleNamespace(campaign=None, events=[], saves=0)

    async def fake_get(uid):
        return state.campaign

    async def fake_save(self):
        state.saves += 1
        return self

    async def fake_record_event(c, type, **payload):
        state.events.append({"type": type, **payload})

    monkeypatch.setattr(campaign_service, "get", fake_get)
    monkeypatch.setattr(Campaign, "save", fake_save)
    monkeypatch.setattr(campaign_service, "record_event", fake_record_event)
    return state


def _plan_stub(monkeypatch, result=None, *, error=None, captured=None):
    async def fake_plan_parts(
        repository_uid, *, template, lens_keys, k, area_prefix=""
    ):
        if captured is not None:
            captured.update(
                repository_uid=repository_uid,
                template=template,
                lens_keys=lens_keys,
                k=k,
                area_prefix=area_prefix,
            )
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(campaign_service, "_plan_parts", fake_plan_parts)


async def test_launch_replaces_parts_and_records_event_when_plan_changed(
    seams, monkeypatch
):
    seams.campaign = _campaign(parts=[_part(0)])
    fresh = [_part(0), _part(1)]
    captured = {}
    _plan_stub(monkeypatch, ([dict(p) for p in fresh], "", "docs"), captured=captured)

    out = await campaign_service.launch("c1", actor_uid="u1")

    assert out.parts == fresh  # DTO carries what will actually run
    assert out.status == "running"
    assert [e["type"] for e in seams.events] == ["replanned", "launched"]
    assert seams.events[0]["parts"] == 2 and seams.events[0]["was"] == 1
    # Replan reuses the campaign's stored inputs — create and launch can't drift.
    assert captured == {
        "repository_uid": "repo1",
        "template": "full",
        "lens_keys": ["bugs"],
        "k": 3,
        "area_prefix": "",
    }


async def test_launch_stays_silent_when_plan_unchanged(seams, monkeypatch):
    original = [_part(0), _part(1)]
    seams.campaign = _campaign(parts=[dict(p) for p in original])
    _plan_stub(monkeypatch, ([dict(p) for p in original], "", "docs"))

    out = await campaign_service.launch("c1")

    assert out.parts == original
    assert [e["type"] for e in seams.events] == ["launched"]


async def test_replan_passes_the_stored_area_prefix_through(seams, monkeypatch):
    seams.campaign = _campaign(area_prefix="backend")
    captured = {}
    _plan_stub(monkeypatch, ([_part(0)], "", "area-map"), captured=captured)

    out = await campaign_service.launch("c1")

    assert captured["area_prefix"] == "backend"
    assert out.area_prefix == "backend"  # DTO passthrough


async def test_degraded_replan_keeps_existing_parts(seams, monkeypatch):
    original = [_part(0)]
    seams.campaign = _campaign(parts=[dict(p) for p in original])
    _plan_stub(monkeypatch, ([], "file tree unavailable (BoomError)", "docs"))

    out = await campaign_service.launch("c1")

    assert out.parts == original  # stale-but-real beats fresh-but-blind
    assert out.status == "running"  # launch itself still proceeds
    assert [e["type"] for e in seams.events] == ["replan_skipped", "launched"]
    assert "file tree unavailable" in seams.events[0]["reason"]


async def test_replan_error_keeps_existing_parts_and_launch_proceeds(
    seams, monkeypatch
):
    original = [_part(0)]
    seams.campaign = _campaign(parts=[dict(p) for p in original])
    _plan_stub(monkeypatch, error=RuntimeError("neo down"))

    out = await campaign_service.launch("c1")

    assert out.parts == original
    assert out.status == "running"
    assert [e["type"] for e in seams.events] == ["replan_skipped", "launched"]
    assert "RuntimeError" in seams.events[0]["reason"]


# ── preview_areas — the no-persist partition preview ─────────────────────────


@pytest.fixture
def preview_seams(monkeypatch):
    """Repository lookup + doc/tree/map loading stubbed; normalize_areas and
    areas_from_map are real. map_inputs=None ⇒ the docs fallback."""
    import domains.repositories.models as repo_models

    state = SimpleNamespace(
        repo=SimpleNamespace(uid="repo1"),
        docs=[],
        tree=([], ""),
        map_inputs=None,
    )

    class _Nodes:
        @staticmethod
        async def get_or_none(**kw):
            return state.repo if kw.get("uid") == "repo1" else None

    monkeypatch.setattr(
        repo_models, "Repository", SimpleNamespace(nodes=_Nodes)
    )

    async def fake_docs(repository_uid):
        return state.docs

    async def fake_tree(repo):
        return state.tree

    async def fake_map_inputs(repository_uid):
        return state.map_inputs

    monkeypatch.setattr(campaign_service, "_doc_inputs", fake_docs)
    monkeypatch.setattr(campaign_service, "_file_tree_paths", fake_tree)
    monkeypatch.setattr(campaign_service, "_area_map_inputs", fake_map_inputs)
    return state


async def test_preview_areas_reports_partition_without_persisting(preview_seams):
    preview_seams.docs = [
        {"uid": "d1", "slug": "backend/api", "title": "API", "watch_paths": ["src/api"]}
    ]
    preview_seams.tree = (["src/api/a.py", "src/api/b.py", "scripts/x.sh"], "")

    out = await campaign_service.preview_areas("repo1")

    assert out["degraded"] == ""
    assert out["total_files"] == 3
    assert out["uncovered_files"] == 1
    titles = [a["title"] for a in out["areas"]]
    assert titles == ["API", "Uncovered paths"]
    assert out["areas"][0]["scope_paths"] == ["src/api"]
    assert out["areas"][0]["file_count"] == 2
    # Docs-derived areas: source "docs", subsystem kind, no keys, no flags.
    assert out["source"] == "docs"
    assert out["oversized_areas"] == []
    assert all(a["kind"] == "subsystem" for a in out["areas"])
    assert all(a["area_key"] == "" for a in out["areas"])
    assert all(a["oversized"] is False for a in out["areas"])


async def test_preview_areas_uses_the_area_map_when_present(preview_seams):
    preview_seams.map_inputs = {
        "subsystem_leaves": [
            {
                "area_key": "backend",
                "title": "Backend",
                "scope_paths": ["src/api"],
                "doc_uids": ["d1"],
            }
        ],
        "features": [
            {
                "area_key": "features/checkout",
                "title": "Checkout",
                "scope_paths": ["src/api/a.py"],
                "doc_uids": [],
            }
        ],
        "ignore_scopes": ["scripts"],
    }
    preview_seams.tree = (["src/api/a.py", "src/api/b.py", "scripts/x.sh"], "")

    out = await campaign_service.preview_areas("repo1")

    assert out["source"] == "area-map"
    assert out["uncovered_files"] == 0  # scripts ignored, src/api covered
    by_key = {a["area_key"]: a for a in out["areas"]}
    assert by_key["backend"]["kind"] == "subsystem"
    assert by_key["backend"]["file_count"] == 2
    # Feature overlays ride along in the preview with their own kind.
    assert by_key["features/checkout"]["kind"] == "feature"
    assert by_key["features/checkout"]["file_count"] == 1
    assert out["oversized_areas"] == []


async def test_preview_areas_features_only_map_plans_from_docs_but_keeps_features(
    preview_seams,
):
    """Areas exist but none are enabled subsystem leaves: the partition
    flips to docs (with the flip explained), yet the map's feature areas
    ride along so full-template campaigns keep their spec-audit parts."""
    preview_seams.docs = [
        {"uid": "d1", "slug": "backend/api", "title": "API", "watch_paths": ["src/api"]}
    ]
    preview_seams.map_inputs = {
        "subsystem_leaves": [],
        "features": [
            {
                "area_key": "features/checkout",
                "title": "Checkout",
                "scope_paths": ["src/api/a.py"],
                "doc_uids": [],
            }
        ],
        "ignore_scopes": [],
    }
    preview_seams.tree = (["src/api/a.py", "src/api/b.py"], "")

    out = await campaign_service.preview_areas("repo1")

    assert out["source"] == "docs"
    assert (
        "area map present but has no enabled subsystem leaves — planned from docs"
        in out["degraded"]
    )
    by_key = {a["area_key"]: a for a in out["areas"]}
    assert by_key["features/checkout"]["kind"] == "feature"
    assert by_key["features/checkout"]["file_count"] == 1
    # The docs partition owns the subsystem side (area_key "" = docs-derived).
    assert by_key[""]["title"] == "API"
    assert by_key[""]["kind"] == "subsystem"


async def test_preview_areas_passes_degraded_reason_through(preview_seams):
    preview_seams.docs = [
        {"uid": "d1", "slug": "backend/api", "title": "API", "watch_paths": ["src/api"]}
    ]
    preview_seams.tree = ([], "no active git provider connection")

    out = await campaign_service.preview_areas("repo1")

    assert out["degraded"] == "no active git provider connection"
    assert out["total_files"] == 0
    assert out["uncovered_files"] == 0
    assert [a["file_count"] for a in out["areas"]] == [None]


async def test_preview_areas_404s_on_unknown_repo(preview_seams):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await campaign_service.preview_areas("nope")
    assert exc.value.status_code == 404
