"""Generate-specs flow — feature-leaf target selection, dispatch seams, and
the base/registry pins.

Mirrors the generate-docs/map-areas templates: the seeded base carries the
task instructions, the tooling contract rides in the structural slot, and
run_generate_specs composes + triggers exactly one "ask" run. DB and dispatch
are monkeypatched; Neo4j-bound behavior stays in integration tests.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from domains.agents.services.registry import AGENT_KEYS, stage_for_agent_key
from domains.agents.services.seed_agent_bases import _AGENT_BASES
from domains.runs.schemas import RunTrigger
from domains.runs.services import sweep
from domains.runs.services.lifecycle import LifecycleError
from domains.runs.services.sweep import (
    _GENERATE_SPECS_TOOLING_CONTRACT,
    GenerateSpecsResult,
    feature_leaf_spec_targets,
    run_generate_specs,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


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

    async def get_or_none(self, uid=None, **_kw):
        return next((r for r in self._rows if r.uid == uid), None)


def _feature(
    key, *, spec="", enabled=True, code_at=None, reviewed_at=None, repo="r1", title=""
):
    return SimpleNamespace(
        uid=key.replace("/", "-"),
        repository_uid=repo,
        key=key,
        kind="feature",
        title=title or key,
        scope_paths=[key],
        spec=spec,
        enabled=enabled,
        code_changed_at=code_at,
        last_reviewed_at=reviewed_at,
    )


# ── base + registry pins ────────────────────────────────────────────────────


def test_generate_specs_agent_exists_and_runs_under_discover():
    assert "generate-specs" in AGENT_KEYS
    assert _AGENT_BASES["generate-specs"]["produces"] == "doc-tree"
    assert stage_for_agent_key("generate-specs", "ask") == "discover"


def test_generate_specs_tooling_contract_names_the_tool():
    flat = " ".join(_GENERATE_SPECS_TOOLING_CONTRACT.split())
    assert "propose_area_edit" in flat
    assert 'kind="feature"' in flat
    assert "pending AreaEdit" in flat


# ── feature_leaf_spec_targets: leaf + no-spec/stale selection ────────────────


async def test_targets_are_no_spec_or_stale_leaves_parents_excluded(monkeypatch):
    rows = [
        # Parent grouping (has a child feature key nesting under it) — NEVER a
        # target even though it has no spec.
        _feature("features/checkout", spec=""),
        # Sub-feature leaf with no spec → target (draft).
        _feature("features/checkout/pay", spec=""),
        # Sub-feature leaf whose spec went stale → target (refresh).
        _feature(
            "features/checkout/refund",
            spec="old",
            code_at=NOW,
            reviewed_at=NOW - timedelta(days=1),
        ),
        # Leaf with a fresh spec → NOT a target.
        _feature(
            "features/search",
            spec="fine",
            code_at=NOW - timedelta(days=2),
            reviewed_at=NOW,
        ),
        # Disabled leaf → excluded.
        _feature("features/legacy", spec="", enabled=False),
        # A subsystem area is never a feature target.
        SimpleNamespace(
            uid="be",
            repository_uid="r1",
            key="backend",
            kind="subsystem",
            title="Backend",
            scope_paths=["be"],
            spec="",
            enabled=True,
            code_changed_at=None,
            last_reviewed_at=None,
        ),
    ]
    monkeypatch.setattr(sweep, "Area", SimpleNamespace(nodes=_Nodes(rows)))
    targets = await feature_leaf_spec_targets("r1")
    assert [a.key for a in targets] == [
        "features/checkout/pay",
        "features/checkout/refund",
    ]


async def test_targets_listing_flags_draft_vs_refresh(monkeypatch):
    draft = _feature("features/a/x", spec="")
    refresh = _feature(
        "features/a/y", spec="old", code_at=NOW, reviewed_at=NOW - timedelta(days=1)
    )
    listing = sweep._feature_spec_targets_listing([draft, refresh])
    assert "no spec — draft" in listing
    assert "stale — refresh" in listing
    assert "features/a/x" in listing and "features/a/y" in listing


# ── run_generate_specs dispatch ─────────────────────────────────────────────


@pytest.fixture
def dispatch_seams(monkeypatch):
    captured = {}

    async def fake_load(agent_uid):
        return None

    async def fake_compose(**kwargs):
        captured["compose"] = kwargs
        return SimpleNamespace(
            text="COMPOSED",
            agent_uid="agentX",
            agent_rev=3,
            composed_degraded=False,
            degraded_layers=(),
        )

    async def fake_trigger(**kwargs):
        captured["trigger"] = kwargs
        return SimpleNamespace(uid="run1")

    async def fake_audit(**kwargs):
        captured["audit"] = kwargs

    monkeypatch.setattr(sweep, "load_agent_prompt_body", fake_load)
    monkeypatch.setattr(
        "domains.agents.services.composition.compose_agent_intent", fake_compose
    )
    monkeypatch.setattr(sweep, "trigger_run", fake_trigger)
    monkeypatch.setattr(sweep, "write_audit", fake_audit)
    return captured


async def test_run_generate_specs_composes_and_triggers_one_ask_run(
    dispatch_seams, monkeypatch
):
    rows = [
        _feature("features/checkout"),  # parent grouping (excluded)
        _feature("features/checkout/pay", spec=""),  # target
    ]
    monkeypatch.setattr(sweep, "Area", SimpleNamespace(nodes=_Nodes(rows)))
    result = await run_generate_specs(repository_uid="r1")
    assert result.run_uid == "run1"
    assert result.targets == ["features/checkout/pay"]
    assert result.errors == []

    compose = dispatch_seams["compose"]
    assert compose["agent_key"] == "generate-specs"
    assert compose["structural"] == _GENERATE_SPECS_TOOLING_CONTRACT
    assert "features/checkout/pay" in compose["existing_state_listing"]

    trigger = dispatch_seams["trigger"]
    assert trigger["playbook"] == "ask"
    assert trigger["stage"] == "discover"
    assert trigger["title"] == "Generate feature specs"
    assert trigger["trigger"] == RunTrigger.MANUAL
    assert dispatch_seams["audit"]["kind"] == "sweep.generate_specs_completed"


async def test_run_generate_specs_gates_on_targets(monkeypatch):
    # Only a fresh-spec leaf → nothing to generate → LifecycleError (409).
    rows = [
        _feature(
            "features/search",
            spec="fine",
            code_at=NOW - timedelta(days=2),
            reviewed_at=NOW,
        )
    ]
    monkeypatch.setattr(sweep, "Area", SimpleNamespace(nodes=_Nodes(rows)))
    with pytest.raises(LifecycleError, match="no feature leaves need a spec"):
        await run_generate_specs(repository_uid="r1")


def test_generate_specs_result_carries_targets():
    result = GenerateSpecsResult(repository_uid="r1")
    assert result.targets == []
