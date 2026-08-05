"""Area-scoped ask runs — run_audit(area_uids=...) narrows the repo-wide
run to the selected areas: their scope paths become Run.target, their specs
the structural contract. DB/dispatch seams monkeypatched, test_sweep_discover
style."""

from types import SimpleNamespace

import pytest

from domains.runs.services import sweep
from domains.runs.services.sweep import run_audit


class _Nodes:
    def __init__(self, rows):
        self._rows = rows

    async def all(self):
        return list(self._rows)

    async def filter(self, **kwargs):
        return [
            r for r in self._rows
            if all(getattr(r, k, None) == v for k, v in kwargs.items())
        ]


def _area(**overrides):
    fields = dict(
        uid="a1",
        repository_uid="r1",
        enabled=True,
        kind="subsystem",
        key="backend",
        title="Backend",
        scope_paths=["back_end"],
        spec="Check the API layer.",
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def dispatch_seams(monkeypatch):
    captured = {}

    async def fake_load(agent_uid):
        return None

    async def fake_workflow(repository_uid, stage):
        return None

    async def fake_compose(**kwargs):
        captured["compose"] = kwargs
        return SimpleNamespace(
            text="COMPOSED",
            agent_uid="agentX",
            agent_rev=2,
            composed_degraded=False,
            degraded_layers=(),
        )

    async def fake_trigger(**kwargs):
        captured["trigger"] = kwargs
        return SimpleNamespace(uid="run1")

    async def fake_audit(**kwargs):
        captured["audit"] = kwargs

    monkeypatch.setattr(sweep, "load_agent_prompt_body", fake_load)
    monkeypatch.setattr(sweep, "_workflow_prompt", fake_workflow)
    monkeypatch.setattr(
        "domains.agents.services.composition.compose_agent_intent", fake_compose
    )
    monkeypatch.setattr(sweep, "trigger_run", fake_trigger)
    monkeypatch.setattr(sweep, "write_audit", fake_audit)
    return captured


async def test_area_scoped_audit_targets_the_areas_paths(dispatch_seams, monkeypatch):
    feature = _area(
        uid="a2",
        kind="feature",
        key="feat/login",
        title="Login",
        scope_paths=["back_end/auth", "front_end/src/auth"],
        spec="Users can log in via OIDC.",
    )
    monkeypatch.setattr(
        sweep, "Area", SimpleNamespace(nodes=_Nodes([_area(), feature]))
    )
    result = await run_audit(
        repository_uid="r1", doc_uids=[], area_uids=["a1", "a2"]
    )
    assert result.runs_dispatched == ["run1"]
    assert result.errors == []
    trigger = dispatch_seams["trigger"]
    assert trigger["target"] == {
        "area_keys": ["backend", "feat/login"],
        "paths": ["back_end", "back_end/auth", "front_end/src/auth"],
    }
    assert trigger["title"].startswith("Audit — ")
    structural = dispatch_seams["compose"]["structural"]
    assert "`backend` (subsystem)" in structural
    assert "`feat/login` (feature)" in structural
    # Feature specs are the contract to verify; subsystem specs are guidance.
    assert "Feature spec" in structural
    assert "Users can log in via OIDC." in structural
    assert "Check the API layer." in structural


async def test_area_scoped_audit_ignores_foreign_and_unknown_areas(
    dispatch_seams, monkeypatch
):
    foreign = _area(uid="a9", repository_uid="OTHER")
    monkeypatch.setattr(sweep, "Area", SimpleNamespace(nodes=_Nodes([foreign])))
    result = await run_audit(
        repository_uid="r1", doc_uids=[], area_uids=["a9", "missing"]
    )
    assert result.runs_dispatched == []
    assert "trigger" not in dispatch_seams  # nothing dispatched
    assert sorted(result.errors) == [
        "area=a9: not found in repository",
        "area=missing: not found in repository",
    ]


async def test_audit_without_areas_keeps_the_whole_repo_scope(
    dispatch_seams, monkeypatch
):
    monkeypatch.setattr(sweep, "Area", SimpleNamespace(nodes=_Nodes([])))
    result = await run_audit(repository_uid="r1", doc_uids=[])
    assert result.runs_dispatched == ["run1"]
    assert dispatch_seams["trigger"]["target"] is None
    assert dispatch_seams["trigger"]["title"] == "Repository audit"
    structural = dispatch_seams["compose"]["structural"]
    assert structural.startswith("The whole repository (no doc-page scoping).")
    # A whole-repo audit carries the coverage contract too. Without it the
    # platform cannot say what fraction of the repository the run reached, and
    # reporting nothing gets recorded as having covered the dispatched scope.
    assert "covered_paths" in structural and "skipped_paths" in structural


async def test_endpoint_rejects_area_uids_combined_with_doc_selection(monkeypatch):
    from fastapi import HTTPException

    import api.v1.sweep as sweep_api
    from api.v1.sweep import AuditRequest
    from domains.users.schemas import UserDTO

    async def allow(repository_uid, org_uid):
        return None

    async def runnable(repository_uid):
        return None

    monkeypatch.setattr(sweep_api, "require_repo_in_org", allow)
    monkeypatch.setattr(sweep_api, "assert_runnable", runnable)

    user = UserDTO(
        uid="u1",
        email="u@example.test",
        display_name="U",
        role="admin",
        org_uid="org-a",
        org_role="owner",
        is_platform_admin=False,
    )
    for req in (
        AuditRequest(area_uids=["a1"], doc_uids=["d1"]),
        AuditRequest(area_uids=["a1"], auto_select=True),
    ):
        with pytest.raises(HTTPException) as exc:
            await sweep_api.run_audit_endpoint("repo-a", req, user=user)
        assert exc.value.status_code == 422
