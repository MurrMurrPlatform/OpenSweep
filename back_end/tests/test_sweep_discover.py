"""Pure tests for the two-stage sweep service.

`run_generate_docs` and `run_audit` touch Neo4j (Doc, ScheduledAgent,
audit log) and the LLM dispatch path, so we exercise them indirectly:
we assert the public surface, the produces mapping, the estimate, and —
with the DB/dispatch seams monkeypatched (test_sweep_map_areas.py style) —
the area-map gate + scaffold on generate-docs. Neo4j-bound behavior lives
in integration tests, not here.
"""

import inspect
from types import SimpleNamespace

import pytest

from domains.agents.services.registry import PRODUCES_TO_PLAYBOOK
from domains.agents.services.seed_agent_bases import _AGENT_BASES
from domains.runs.services import sweep
from domains.runs.services.lifecycle import LifecycleError
from domains.runs.services.sweep import (
    estimate_sweep_cost,
    run_audit,
    run_generate_docs,
)


class _Nodes:
    def __init__(self, rows):
        self._rows = rows

    async def all(self):
        return list(self._rows)


def test_generate_docs_agent_produces_the_doc_tree():
    assert _AGENT_BASES["generate-docs"]["produces"] == "doc-tree"
    assert PRODUCES_TO_PLAYBOOK["doc-tree"] == "ask"


def test_run_audit_has_no_concern_taxonomy_parameter():
    params = inspect.signature(run_audit).parameters
    assert "concerns" not in params
    assert "custom_intent" in params


def test_estimate_reports_two_stage_shape():
    estimate = estimate_sweep_cost(7)
    assert estimate["docs"] == 7
    assert estimate["generate_docs_runs"] == 1
    assert estimate["audit_runs_if_all_selected"] == 7
    assert "note" in estimate


# ── docs gate + area-map scaffold ───────────────────────────────────────────


def _subsystem_area(**overrides):
    fields = dict(
        repository_uid="r1",
        enabled=True,
        kind="subsystem",
        key="backend",
        title="Backend",
        scope_paths=["back_end"],
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

    async def fake_pages(repository_uid):
        return "(none yet — this is the first Generate docs for this repository)"

    async def fake_areas(repository_uid):
        return "- backend [subsystem] Backend :: back_end"

    async def fake_compose(**kwargs):
        captured["compose"] = kwargs
        return SimpleNamespace(text="COMPOSED", agent_uid="agentX", agent_rev=2)

    async def fake_trigger(**kwargs):
        captured["trigger"] = kwargs
        return SimpleNamespace(uid="run1")

    async def fake_audit(**kwargs):
        captured["audit"] = kwargs

    monkeypatch.setattr(sweep, "load_agent_prompt_body", fake_load)
    monkeypatch.setattr(sweep, "_workflow_prompt", fake_workflow)
    monkeypatch.setattr(sweep, "_existing_pages_listing", fake_pages)
    monkeypatch.setattr(sweep, "_existing_areas_listing", fake_areas)
    monkeypatch.setattr(
        "domains.agents.services.composition.compose_agent_intent", fake_compose
    )
    monkeypatch.setattr(sweep, "trigger_run", fake_trigger)
    monkeypatch.setattr(sweep, "write_audit", fake_audit)
    return captured


async def test_generate_docs_gated_without_an_area_map(dispatch_seams, monkeypatch):
    monkeypatch.setattr(sweep, "Area", SimpleNamespace(nodes=_Nodes([])))
    with pytest.raises(LifecycleError, match="no area map — run Map areas first"):
        await run_generate_docs(repository_uid="r1")
    assert "trigger" not in dispatch_seams  # nothing dispatched


@pytest.mark.parametrize(
    "area",
    [
        _subsystem_area(enabled=False),  # disabled subsystem doesn't count
        _subsystem_area(kind="feature"),  # features don't partition the tree
        _subsystem_area(kind="ignore"),
        _subsystem_area(repository_uid="OTHER"),  # someone else's map
    ],
)
async def test_generate_docs_gate_ignores_non_qualifying_areas(
    dispatch_seams, monkeypatch, area
):
    monkeypatch.setattr(sweep, "Area", SimpleNamespace(nodes=_Nodes([area])))
    with pytest.raises(LifecycleError, match="no area map"):
        await run_generate_docs(repository_uid="r1")


async def test_generate_docs_dispatches_with_the_area_map_scaffold(
    dispatch_seams, monkeypatch
):
    monkeypatch.setattr(
        sweep, "Area", SimpleNamespace(nodes=_Nodes([_subsystem_area()]))
    )
    result = await run_generate_docs(repository_uid="r1")
    assert result.run_uid == "run1"
    assert result.errors == []
    compose = dispatch_seams["compose"]
    assert compose["agent_key"] == "generate-docs"
    # The Area-map metadata scaffolds the page tree — structural slot, after
    # the tooling contract (mirror of map-areas' docs listing).
    assert "## Area map (metadata)" in compose["structural"]
    assert "- backend [subsystem] Backend :: back_end" in compose["structural"]
    assert compose["structural"].index("propose_doc_edit") < compose[
        "structural"
    ].index("## Area map (metadata)")


def test_generate_docs_body_defaults_the_tree_to_the_area_map():
    flat = " ".join(_AGENT_BASES["generate-docs"]["body"].split())
    assert "An Area map exists for this repository" in flat
    assert "one page per subsystem area" in flat
    assert "watch_paths inherited from the area's scope" in flat
    assert "spans or splits areas" in flat


def test_map_areas_seeds_before_keep_docs_current():
    """The area map gates docs generation, so its bindings seed first."""
    from domains.repositories.services.registration import seed_repo_defaults

    src = inspect.getsource(seed_repo_defaults)
    assert 0 < src.index('"map-areas"') < src.index('"keep-docs-current"')


async def test_generate_docs_endpoint_409s_on_the_gate(monkeypatch):
    from fastapi import HTTPException

    import api.v1.sweep as sweep_api
    import domains.agents.services.registry as registry
    from domains.users.schemas import UserDTO

    async def allow(repository_uid, org_uid):
        return None

    async def runnable(repository_uid):
        return None

    async def no_agent(key):
        return None

    async def no_runs(*, repository_uid):
        return []

    async def gated(**kwargs):
        raise LifecycleError(
            "no area map — run Map areas first (docs are scaffolded by the "
            "area partition)"
        )

    monkeypatch.setattr(sweep_api, "require_repo_in_org", allow)
    monkeypatch.setattr(sweep_api, "assert_runnable", runnable)
    monkeypatch.setattr(registry, "system_agent_by_key", no_agent)
    monkeypatch.setattr(sweep_api, "active_runs_for", no_runs)
    monkeypatch.setattr(sweep_api, "run_generate_docs", gated)

    user = UserDTO(
        uid="u1",
        email="u@example.test",
        display_name="U",
        role="admin",
        org_uid="org-a",
        org_role="owner",
        is_platform_admin=False,
    )
    with pytest.raises(HTTPException) as exc:
        await sweep_api.run_generate_docs_endpoint("repo-a", user=user)
    assert exc.value.status_code == 409
    assert "no area map" in str(exc.value.detail)
