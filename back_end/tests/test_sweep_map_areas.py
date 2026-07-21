"""Map-areas flow — base/registry pins, prompt listings, dispatch seams.

Mirrors the generate-docs template: the seeded base carries the task
instructions, the tooling contract rides in the structural slot, and
run_map_areas composes + triggers exactly one "ask" run. DB and dispatch
are monkeypatched (test_campaign_scanner.py style); Neo4j-bound behavior
stays in integration tests.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from domains.agents.services import dispatch, schedule_scanner
from domains.agents.services.registry import (
    AGENT_KEYS,
    PRODUCES_TO_PLAYBOOK,
    stage_for_agent_key,
)
from domains.agents.services.seed_agent_bases import _AGENT_BASES
from domains.runs.schemas import RunTrigger
from domains.runs.services import sweep
from domains.runs.services.lifecycle import LifecycleError
from domains.runs.services.sweep import (
    _MAP_AREAS_TOOLING_CONTRACT,
    MapAreasResult,
    run_map_areas,
)


class _Nodes:
    def __init__(self, rows):
        self._rows = rows

    async def all(self):
        return list(self._rows)

    async def get_or_none(self, uid=None, **_kw):
        return next((r for r in self._rows if r.uid == uid), None)


# ── base + registry pins ────────────────────────────────────────────────────


def test_map_areas_agent_exists_and_produces_the_doc_tree():
    assert "map-areas" in AGENT_KEYS
    assert _AGENT_BASES["map-areas"]["produces"] == "doc-tree"
    assert PRODUCES_TO_PLAYBOOK["doc-tree"] == "ask"


def test_map_areas_runs_under_the_discover_stage():
    assert stage_for_agent_key("map-areas", "ask") == "discover"


def test_map_areas_body_carries_the_boundary_tests():
    body = _AGENT_BASES["map-areas"]["body"]
    flat = " ".join(body.split())
    assert "# Boundary tests" in body
    assert "ONE sentence without 'and also'" in flat
    assert "crossing traffic is narrow" in flat
    assert "files that change together belong together" in flat
    assert "distinct mindset from its neighbors" in flat


def test_map_areas_body_prefers_nested_children_over_dash_compounds():
    flat = " ".join(_AGENT_BASES["map-areas"]["body"].split())
    assert "child keys under a scopeless parent" in flat
    assert '"backend/domains-delivery"' in flat
    assert "bundles small sibling leaves into one run automatically" in flat


def test_map_areas_body_sets_the_feature_bar():
    flat = " ".join(_AGENT_BASES["map-areas"]["body"].split())
    assert "spans at least two subsystems" in flat
    assert "never create feature echoes of single subsystems" in flat
    assert "infrastructure plumbing" in flat


def test_map_areas_body_links_docs_via_doc_uids():
    flat = " ".join(_AGENT_BASES["map-areas"]["body"].split())
    assert "doc_uids" in flat
    assert "the metadata listing carries each page's uid" in flat


def test_tooling_contract_names_the_tools():
    flat = " ".join(_MAP_AREAS_TOOLING_CONTRACT.split())
    assert "propose_area_edit" in flat
    assert "read_doc" in flat
    assert "subsystem | feature | ignore" in flat
    # In-loop feedback contract: warnings come back from the tool and the
    # agent must resolve them; enabled=false retires an area.
    assert "warnings" in flat
    assert "enabled=false" in flat


# ── listings ────────────────────────────────────────────────────────────────


async def test_existing_areas_listing_shape(monkeypatch):
    areas = [
        SimpleNamespace(
            repository_uid="r1",
            enabled=True,
            key="backend/delivery",
            kind="subsystem",
            title="Delivery",
            scope_paths=["back_end/a", "back_end/b", "back_end/c", "back_end/d"],
        ),
        SimpleNamespace(
            repository_uid="r1",
            enabled=True,
            key="backend",
            kind="subsystem",
            title="Backend",
            scope_paths=[],
        ),
        SimpleNamespace(
            repository_uid="r1",
            enabled=False,
            key="disabled-area",
            kind="ignore",
            title="Disabled",
            scope_paths=[],
        ),
        SimpleNamespace(
            repository_uid="OTHER",
            enabled=True,
            key="other-repo",
            kind="subsystem",
            title="Other",
            scope_paths=[],
        ),
    ]
    monkeypatch.setattr(sweep, "Area", SimpleNamespace(nodes=_Nodes(areas)))
    out = await sweep._existing_areas_listing("r1")
    lines = out.splitlines()
    # Sorted by key; one line per enabled area of THIS repo.
    assert lines[0].startswith("- backend [subsystem] Backend")
    assert "- backend/delivery [subsystem] Delivery :: back_end/a, back_end/b, back_end/c" in out
    assert "back_end/d" not in out  # first 3 scope paths only
    assert "disabled-area" not in out
    assert "other-repo" not in out


async def test_existing_areas_listing_first_run_fallback(monkeypatch):
    monkeypatch.setattr(sweep, "Area", SimpleNamespace(nodes=_Nodes([])))
    out = await sweep._existing_areas_listing("r1")
    assert out == "(none yet — this is the first Map areas run)"


async def test_docs_metadata_listing_never_leaks_bodies(monkeypatch):
    docs = [
        SimpleNamespace(
            uid="doc-queues",
            repository_uid="r1",
            slug="backend/queues",
            title="Queue workers",
            watch_paths=["back_end/domains/queues/"],
            body="DISTINCTIVE-BODY-CANARY the whole page text",
            summary="SECRET-SUMMARY-CANARY",
        ),
        SimpleNamespace(
            uid="doc-other",
            repository_uid="OTHER",
            slug="other",
            title="Other repo",
            watch_paths=[],
            body="",
            summary="",
        ),
    ]
    monkeypatch.setattr(sweep, "Doc", SimpleNamespace(nodes=_Nodes(docs)))
    out = await sweep._docs_metadata_listing("r1")
    # The uid rides along so area proposals can link pages via doc_uids.
    assert (
        "- backend/queues (uid=doc-queues): Queue workers "
        ":: watch_paths: back_end/domains/queues/" in out
    )
    # Anti-circularity: metadata only — bodies/summaries ride read_doc.
    assert "DISTINCTIVE-BODY-CANARY" not in out
    assert "SECRET-SUMMARY-CANARY" not in out
    assert "other" not in out


async def test_docs_metadata_listing_empty_fallback(monkeypatch):
    monkeypatch.setattr(sweep, "Doc", SimpleNamespace(nodes=_Nodes([])))
    assert await sweep._docs_metadata_listing("r1") == "(no documentation pages yet)"


# ── run_map_areas dispatch ──────────────────────────────────────────────────

AREAS_LISTING = "(none yet — this is the first Map areas run)"
DOCS_LISTING = "- guide: Guide :: watch_paths: back_end/"


@pytest.fixture
def dispatch_seams(monkeypatch):
    captured = {}

    async def fake_load(agent_uid):
        return None

    async def fake_workflow(repository_uid, stage):
        # run_map_areas must NEVER consult the workflow stage prompt: the
        # "discover" stage guidance is generate-docs prose ("build the
        # documentation page tree via propose_doc_edit") — composing it in
        # gave the mapping agent two conflicting briefs (observed in prod:
        # a Map-areas run filing 25 DocEdits, 0 AreaEdits).
        raise AssertionError("map-areas must not fall back to the workflow prompt")

    async def fake_areas(repository_uid):
        return AREAS_LISTING

    async def fake_docs(repository_uid):
        return DOCS_LISTING

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
    monkeypatch.setattr(sweep, "_existing_areas_listing", fake_areas)
    monkeypatch.setattr(sweep, "_docs_metadata_listing", fake_docs)
    monkeypatch.setattr(
        "domains.agents.services.composition.compose_agent_intent", fake_compose
    )
    monkeypatch.setattr(sweep, "trigger_run", fake_trigger)
    monkeypatch.setattr(sweep, "write_audit", fake_audit)
    return captured


async def test_run_map_areas_composes_and_triggers_one_ask_run(dispatch_seams):
    result = await run_map_areas(repository_uid="r1")
    assert result.run_uid == "run1"
    assert result.errors == []
    assert result.summary == "Map areas: 1 LLM run dispatched"

    compose = dispatch_seams["compose"]
    assert compose["agent_key"] == "map-areas"
    assert compose["existing_state_listing"] == AREAS_LISTING
    # Tooling contract + doc-tree metadata ride the structural slot.
    assert compose["structural"].startswith(_MAP_AREAS_TOOLING_CONTRACT)
    assert "## Doc tree (metadata)" in compose["structural"]
    assert DOCS_LISTING in compose["structural"]

    # No workflow-prompt fallback: prompt_body stays None (agent override only).
    assert compose["prompt_body"] is None

    trigger = dispatch_seams["trigger"]
    assert trigger["intent"] == "COMPOSED"
    assert trigger["playbook"] == "ask"
    assert trigger["stage"] == "discover"
    assert trigger["title"] == "Map areas"
    assert trigger["agent_uid"] == "agentX"
    assert trigger["agent_rev"] == 2
    assert trigger["trigger"] == RunTrigger.MANUAL
    assert trigger["triggered_by"] == "map-areas"

    assert dispatch_seams["audit"]["kind"] == "sweep.map_areas_dispatched"


async def test_run_map_areas_captures_dispatch_failure(dispatch_seams, monkeypatch):
    async def boom(**_kw):
        raise LifecycleError("no provider configured")

    monkeypatch.setattr(sweep, "trigger_run", boom)
    result = await run_map_areas(repository_uid="r1")
    assert result.run_uid == ""
    assert any("no provider configured" in e for e in result.errors)
    assert result.summary == "Map areas: no run dispatched"
    assert dispatch_seams["audit"]["payload"]["errors"] == 1


# ── schedule scanner branch ─────────────────────────────────────────────────

NOW = datetime(2026, 8, 1, 5, 30, tzinfo=timezone.utc)  # the 1st, 05:30


class _FakeSA(SimpleNamespace):
    async def save(self):
        self.saved = True


@pytest.fixture
def scanner_seams(monkeypatch):
    captured = {}
    sa = _FakeSA(
        uid="sa1",
        agent_uid="agent1",
        repository_uid="repo1",
        title="Monthly area-map refresh",
        trigger="cron:0 5 1 * *",
        target={},
        autonomy="ask-before-run",
        enabled=True,
        last_scheduled_at=None,
        saved=False,
    )
    agent = SimpleNamespace(uid="agent1", source_url="opensweep://agent/map-areas")
    monkeypatch.setattr(
        schedule_scanner, "ScheduledAgent", SimpleNamespace(nodes=_Nodes([sa]))
    )
    monkeypatch.setattr(
        schedule_scanner, "Agent", SimpleNamespace(nodes=_Nodes([agent]))
    )

    async def fake_run_map_areas(**kwargs):
        captured["run_map_areas"] = kwargs
        return MapAreasResult(repository_uid=kwargs["repository_uid"], run_uid="run1")

    async def fake_in_flight(repository_uid):
        captured["in_flight_checked"] = repository_uid
        return captured.get("in_flight_run")

    monkeypatch.setattr(sweep, "run_map_areas", fake_run_map_areas)
    monkeypatch.setattr(sweep, "map_areas_run_in_flight", fake_in_flight)
    captured["sa"] = sa
    return captured


async def test_due_map_areas_binding_dispatches_and_stamps(scanner_seams):
    result = await schedule_scanner.scan_and_dispatch(now=NOW)
    assert result.dispatched == 1
    assert result.errors == []
    call = scanner_seams["run_map_areas"]
    assert call["repository_uid"] == "repo1"
    assert call["triggered_by"] == "cron:0 5 1 * *"
    assert call["trigger"] == RunTrigger.SCHEDULE
    assert scanner_seams["sa"].last_scheduled_at == NOW
    assert scanner_seams["sa"].saved is True


async def test_disabled_autonomy_skips_but_stamps(scanner_seams):
    scanner_seams["sa"].autonomy = "disabled"
    result = await schedule_scanner.scan_and_dispatch(now=NOW)
    assert result.dispatched == 0
    assert "run_map_areas" not in scanner_seams
    assert scanner_seams["sa"].last_scheduled_at == NOW  # tick consumed


async def test_dispatch_failure_is_an_error_and_still_stamps(scanner_seams, monkeypatch):
    """Failure consumes the tick: monthly maintenance must not retry every
    beat for a month — the next cron slot (or a manual click) is the retry."""

    async def failed(**kwargs):
        return MapAreasResult(
            repository_uid=kwargs["repository_uid"],
            errors=["map-areas: provider exploded"],
        )

    monkeypatch.setattr(sweep, "run_map_areas", failed)
    result = await schedule_scanner.scan_and_dispatch(now=NOW)
    assert result.dispatched == 0
    assert any("provider exploded" in e for e in result.errors)
    assert scanner_seams["sa"].last_scheduled_at == NOW  # tick consumed anyway


async def test_dispatch_exception_is_an_error_and_still_stamps(
    scanner_seams, monkeypatch
):
    async def boom(**kwargs):
        raise RuntimeError("neo down")

    monkeypatch.setattr(sweep, "run_map_areas", boom)
    result = await schedule_scanner.scan_and_dispatch(now=NOW)
    assert result.dispatched == 0
    assert any("RuntimeError" in e for e in result.errors)
    assert scanner_seams["sa"].last_scheduled_at == NOW  # tick consumed anyway


async def test_in_flight_run_skips_the_tick_but_stamps(scanner_seams):
    scanner_seams["in_flight_run"] = SimpleNamespace(uid="run-busy")
    result = await schedule_scanner.scan_and_dispatch(now=NOW)
    assert result.dispatched == 0
    assert result.errors == []
    assert "run_map_areas" not in scanner_seams  # nothing double-proposed
    assert scanner_seams["in_flight_checked"] == "repo1"
    assert scanner_seams["sa"].last_scheduled_at == NOW  # tick consumed


# ── trigger_scheduled_agent routing ─────────────────────────────────────────


@pytest.fixture
def trigger_seams(monkeypatch):
    captured = {}
    sa = SimpleNamespace(
        uid="sa1",
        agent_uid="agent1",
        repository_uid="repo1",
        target={},
        effort="",
        run_policy_uid="",
        title="Map areas",
    )
    agent = SimpleNamespace(
        uid="agent1",
        source_url="opensweep://agent/map-areas",
        enabled=True,
        produces="doc-tree",
        provenance="system",
        prompt="",
        title="OpenSweep agent — Map areas",
    )
    run_node = SimpleNamespace(uid="run1")
    monkeypatch.setattr(
        dispatch, "ScheduledAgent", SimpleNamespace(nodes=_Nodes([sa]))
    )
    monkeypatch.setattr(dispatch, "Agent", SimpleNamespace(nodes=_Nodes([agent])))
    monkeypatch.setattr(
        "domains.runs.models.Run", SimpleNamespace(nodes=_Nodes([run_node]))
    )

    async def fake_run_map_areas(**kwargs):
        captured["run_map_areas"] = kwargs
        return MapAreasResult(repository_uid=kwargs["repository_uid"], run_uid="run1")

    monkeypatch.setattr(sweep, "run_map_areas", fake_run_map_areas)
    captured["run_node"] = run_node
    return captured


async def test_trigger_scheduled_agent_routes_map_areas_to_the_sweep(trigger_seams):
    run = await dispatch.trigger_scheduled_agent(
        "sa1", trigger=RunTrigger.MANUAL, triggered_by="user1"
    )
    assert run is trigger_seams["run_node"]
    call = trigger_seams["run_map_areas"]
    assert call["repository_uid"] == "repo1"
    assert call["triggered_by"] == "user1"
    assert call["trigger"] == RunTrigger.MANUAL


async def test_trigger_scheduled_agent_raises_on_map_areas_failure(
    trigger_seams, monkeypatch
):
    async def failed(**kwargs):
        return MapAreasResult(
            repository_uid=kwargs["repository_uid"],
            errors=["map-areas: no provider configured"],
        )

    monkeypatch.setattr(sweep, "run_map_areas", failed)
    with pytest.raises(LifecycleError, match="no provider configured"):
        await dispatch.trigger_scheduled_agent("sa1")


# ── seeding shape ───────────────────────────────────────────────────────────


def test_seeded_map_areas_binding_shape():
    import inspect

    from domains.agents.services.scheduled_agent_service import (
        MAP_AREAS_MONTHLY_TITLE,
        MAP_AREAS_TITLE,
        seed_map_areas,
    )

    assert MAP_AREAS_TITLE == "Map areas"
    assert MAP_AREAS_MONTHLY_TITLE == "Monthly area-map refresh"
    src = inspect.getsource(seed_map_areas)
    # Manual anchor is inert-but-enabled; the monthly cron is seeded DISABLED.
    assert '(MAP_AREAS_TITLE, "", True)' in src
    assert '(MAP_AREAS_MONTHLY_TITLE, "cron:0 5 1 * *", False)' in src
