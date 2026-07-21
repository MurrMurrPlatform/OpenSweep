"""Areas API surface — routes mounted, accept carries partition warnings,
bulk response shapes, and the map-areas dispatch endpoint (lazy-import seam
for run_map_areas + the per-repository in-flight 409 guard). DB-free.

Deliberately does NOT import domains.runs.services.sweep at module scope:
the map-areas orchestration lands separately, and the endpoint's lazy import
is exactly the seam these tests monkeypatch.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api.v1.areas as areas_mod
from app import app
from domains.areas.models import Area
from domains.areas.services import area_service
from domains.users.schemas import UserDTO


def _openapi_paths() -> set[str]:
    # app.routes holds lazy _IncludedRouter entries until openapi() renders,
    # so assert against the schema paths (same as test_phase3_routes).
    return set(app.openapi().get("paths", {}).keys())


def _openapi_operation_ids() -> set[str]:
    schema = app.openapi()
    ops = set()
    for methods in schema.get("paths", {}).values():
        for op in methods.values():
            if isinstance(op, dict) and op.get("operationId"):
                ops.add(op["operationId"])
    return ops


def test_areas_routes_are_mounted():
    paths = _openapi_paths()
    for path in (
        "/api/v1/areas",
        "/api/v1/areas/{uid}",
        "/api/v1/areas/{uid}/detail",
        "/api/v1/area-edits",
        "/api/v1/area-edits/{uid}/accept",
        "/api/v1/area-edits/{uid}/reject",
        "/api/v1/area-edits/bulk-accept",
        "/api/v1/area-edits/bulk-reject",
        "/api/v1/repositories/{repository_uid}/sweep/map-areas",
        "/api/v1/repositories/{repository_uid}/areas/reset",
    ):
        assert path in paths, f"missing areas route {path}"


def test_areas_operation_ids():
    ops = _openapi_operation_ids()
    for op_id in (
        "opensweep_list_areas",
        "opensweep_get_area",
        "opensweep_area_detail",
        "opensweep_update_area",
        "opensweep_delete_area",
        "opensweep_list_area_edits",
        "opensweep_accept_area_edit",
        "opensweep_reject_area_edit",
        "opensweep_bulk_accept_area_edits",
        "opensweep_bulk_reject_area_edits",
        "opensweep_run_map_areas",
    ):
        assert op_id in ops, f"missing areas operation {op_id}"


# ── Fixtures / fakes ────────────────────────────────────────────────────────


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


def _area(uid="a1", key="backend/delivery") -> Area:
    return Area(uid=uid, repository_uid="repo-a", key=key, kind="subsystem", title="Delivery")


@pytest.fixture(autouse=True)
def _tenancy_noop(monkeypatch):
    async def allow(repository_uid, org_uid):
        return None

    monkeypatch.setattr(areas_mod, "require_repo_in_org", allow)


# ── Accept carries warnings ─────────────────────────────────────────────────


async def test_accept_response_carries_warnings(monkeypatch):
    edit = SimpleNamespace(uid="e1", repository_uid="repo-a")

    async def fake_get_area_edit(uid):
        assert uid == "e1"
        return edit

    async def fake_accept(uid, *, actor):
        assert actor == "u1"
        return _area(), ["scope 'x/' overlaps leaf 'backend/api' ('x/')"]

    monkeypatch.setattr(area_service, "get_area_edit", fake_get_area_edit)
    monkeypatch.setattr(area_service, "accept_area_edit", fake_accept)

    resp = await areas_mod.accept_area_edit("e1", user=_user())
    assert resp.area.key == "backend/delivery"
    assert resp.warnings == ["scope 'x/' overlaps leaf 'backend/api' ('x/')"]


# ── Bulk shapes ─────────────────────────────────────────────────────────────


async def test_bulk_accept_reports_per_uid_warnings_and_errors(monkeypatch):
    async def fake_get_area_edit(uid):
        return SimpleNamespace(uid=uid, repository_uid="repo-a")

    async def fake_accept(uid, *, actor):
        if uid == "bad":
            raise HTTPException(status_code=409, detail="AreaEdit is rejected, not pending")
        warnings = ["overlap warning"] if uid == "warned" else []
        return _area(uid=f"area-{uid}"), warnings

    monkeypatch.setattr(area_service, "get_area_edit", fake_get_area_edit)
    monkeypatch.setattr(area_service, "accept_area_edit", fake_accept)

    from domains.areas.schemas import BulkAreaEditRequest

    result = await areas_mod.bulk_accept_area_edits(
        BulkAreaEditRequest(uids=["clean", "warned", "bad"]), user=_user()
    )
    assert result["accepted"] == ["clean", "warned"]
    assert result["warnings"] == {"warned": ["overlap warning"]}
    assert result["errors"] == {"bad": "AreaEdit is rejected, not pending"}


async def test_bulk_reject_shape(monkeypatch):
    async def fake_get_area_edit(uid):
        return SimpleNamespace(uid=uid, repository_uid="repo-a")

    async def fake_reject(uid, *, actor):
        if uid == "bad":
            raise HTTPException(status_code=409, detail="already resolved")
        return SimpleNamespace(uid=uid)

    monkeypatch.setattr(area_service, "get_area_edit", fake_get_area_edit)
    monkeypatch.setattr(area_service, "reject_area_edit", fake_reject)

    from domains.areas.schemas import BulkAreaEditRequest

    result = await areas_mod.bulk_reject_area_edits(
        BulkAreaEditRequest(uids=["ok", "bad"]), user=_user()
    )
    assert result["rejected"] == ["ok"]
    assert result["errors"] == {"bad": "already resolved"}


# ── Map-areas dispatch ──────────────────────────────────────────────────────


def _patch_map_areas_env(monkeypatch, *, agent=None, active_runs=()):
    async def fake_assert_runnable(repository_uid):
        return None

    async def fake_active_runs_for(*, repository_uid):
        return list(active_runs)

    async def fake_system_agent_by_key(key):
        assert key == "map-areas"
        return agent

    monkeypatch.setattr(areas_mod, "assert_runnable", fake_assert_runnable)

    # The guard lives in sweep.map_areas_run_in_flight now (shared with the
    # schedule scanner); it resolves these two seams lazily at call time.
    import domains.agents.services.registry as registry
    import domains.runs.services.active_runs as active_runs_mod

    monkeypatch.setattr(registry, "system_agent_by_key", fake_system_agent_by_key)
    monkeypatch.setattr(active_runs_mod, "active_runs_for", fake_active_runs_for)


async def test_map_areas_dispatches_via_lazy_import_seam(monkeypatch):
    _patch_map_areas_env(monkeypatch, agent=SimpleNamespace(uid="agent-map"))

    called = {}

    async def fake_run_map_areas(*, repository_uid, triggered_by="", **kwargs):
        called["repository_uid"] = repository_uid
        called["triggered_by"] = triggered_by
        return SimpleNamespace(
            repository_uid=repository_uid, run_uid="run-42", errors=[], summary="mapped"
        )

    import domains.runs.services.sweep as sweep_mod

    monkeypatch.setattr(sweep_mod, "run_map_areas", fake_run_map_areas, raising=False)

    result = await areas_mod.run_map_areas_endpoint("repo-a", user=_user())
    assert called == {"repository_uid": "repo-a", "triggered_by": "u1"}
    assert result.run_uid == "run-42"
    assert result.summary == "mapped"
    assert result.errors == []


async def test_map_areas_409_when_in_flight(monkeypatch):
    agent = SimpleNamespace(uid="agent-map")
    in_flight = SimpleNamespace(uid="run-busy", agent_uid="agent-map", scheduled_agent_uid="")
    _patch_map_areas_env(monkeypatch, agent=agent, active_runs=[in_flight])

    async def must_not_dispatch(**kwargs):  # pragma: no cover - guard must fire first
        raise AssertionError("run_map_areas dispatched despite in-flight guard")

    import domains.runs.services.sweep as sweep_mod

    monkeypatch.setattr(sweep_mod, "run_map_areas", must_not_dispatch, raising=False)

    with pytest.raises(HTTPException) as exc:
        await areas_mod.run_map_areas_endpoint("repo-a", user=_user())
    assert exc.value.status_code == 409
    assert exc.value.detail["run_uid"] == "run-busy"


async def test_map_areas_guard_inert_when_agent_unseeded_still_dispatches(monkeypatch):
    """No seeded map-areas agent ⇒ the guard is inert (logged), never a 409 —
    a missing seed must not block mapping."""
    busy = SimpleNamespace(uid="run-busy", agent_uid="agent-map", scheduled_agent_uid="")
    _patch_map_areas_env(monkeypatch, agent=None, active_runs=[busy])

    async def fake_run_map_areas(*, repository_uid, triggered_by="", **kwargs):
        return SimpleNamespace(repository_uid=repository_uid, run_uid="run-9", errors=[], summary="")

    import domains.runs.services.sweep as sweep_mod

    monkeypatch.setattr(sweep_mod, "run_map_areas", fake_run_map_areas, raising=False)
    warnings: list[str] = []
    monkeypatch.setattr(
        sweep_mod,
        "logger",
        SimpleNamespace(warning=lambda msg, **kw: warnings.append(msg)),
    )

    result = await areas_mod.run_map_areas_endpoint("repo-a", user=_user())
    assert result.run_uid == "run-9"
    assert any(
        "map-areas in-flight guard inactive for repo-a" in w for w in warnings
    )


async def test_map_areas_409_when_kill_switch_active(monkeypatch):
    _patch_map_areas_env(monkeypatch, agent=None)

    from infrastructure.kill_switch import KillSwitchActiveError

    async def blocked(repository_uid):
        raise KillSwitchActiveError("kill switch active")

    monkeypatch.setattr(areas_mod, "assert_runnable", blocked)

    with pytest.raises(HTTPException) as exc:
        await areas_mod.run_map_areas_endpoint("repo-a", user=_user())
    assert exc.value.status_code == 409


async def test_map_areas_other_agents_runs_do_not_block(monkeypatch):
    """Only map-areas runs trip the guard — an unrelated active run must not."""
    agent = SimpleNamespace(uid="agent-map")
    other_run = SimpleNamespace(uid="run-other", agent_uid="agent-deep-scan", scheduled_agent_uid="")
    _patch_map_areas_env(monkeypatch, agent=agent, active_runs=[other_run])

    async def fake_run_map_areas(*, repository_uid, triggered_by="", **kwargs):
        return SimpleNamespace(repository_uid=repository_uid, run_uid="run-1", errors=[], summary="")

    import domains.runs.services.sweep as sweep_mod

    monkeypatch.setattr(sweep_mod, "run_map_areas", fake_run_map_areas, raising=False)

    result = await areas_mod.run_map_areas_endpoint("repo-a", user=_user())
    assert result.run_uid == "run-1"


def test_reset_endpoints_are_registered():
    ops = _openapi_operation_ids()
    assert "opensweep_reset_areas" in ops
    assert "opensweep_reset_docs" in ops
