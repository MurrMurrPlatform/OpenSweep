"""revise_area_spec — one-shot spec revision via LLM.

Tests that:
- A missing area_uid raises LifecycleError.
- With a real area (monkeypatched), exactly one "ask" run is dispatched whose
  intent contains both the instruction text and the area's current spec.
- The seeded "revise-spec" agent base exists and is well-formed.

Mirrors test_sweep_generate_specs.py in fixture/monkeypatch style.
"""

from types import SimpleNamespace

import pytest

from domains.agents.services.registry import AGENT_KEYS, stage_for_agent_key
from domains.agents.services.seed_agent_bases import _AGENT_BASES
from domains.runs.schemas import RunTrigger
from domains.runs.services import sweep
from domains.runs.services.lifecycle import LifecycleError
from domains.runs.services.sweep import (
    _GENERATE_SPECS_TOOLING_CONTRACT,
    ReviseSpecResult,
    revise_area_spec,
)


# ── helpers ──────────────────────────────────────────────────────────────────


class _Nodes:
    def __init__(self, rows):
        self._rows = rows

    async def all(self):
        return list(self._rows)

    async def get_or_none(self, uid=None, **_kw):
        return next((r for r in self._rows if r.uid == uid), None)


def _area(uid, *, key="features/auth", title="Auth", spec="existing spec", scope_paths=None, repo="r1"):
    return SimpleNamespace(
        uid=uid,
        repository_uid=repo,
        key=key,
        kind="feature",
        title=title,
        scope_paths=scope_paths or [key],
        spec=spec,
        enabled=True,
    )


# ── agent base / registry pins ───────────────────────────────────────────────


def test_revise_spec_agent_exists_and_runs_under_discover():
    assert "revise-spec" in AGENT_KEYS
    assert _AGENT_BASES["revise-spec"]["produces"] == "doc-tree"
    assert stage_for_agent_key("revise-spec", "ask") == "discover"


def test_revise_spec_base_is_well_formed():
    spec = _AGENT_BASES["revise-spec"]
    assert spec["title"]
    assert spec["description"]
    assert spec["body"].strip()
    assert "opensweep-agent-base" in spec["tags"]
    assert "revise-spec" in spec["tags"]
    # Must instruct to propose exactly one call.
    assert "propose_area_edit" in spec["body"]
    # Must mention the maintainer instruction.
    assert "maintainer" in spec["body"].lower()


# ── missing-area guard ───────────────────────────────────────────────────────


async def test_revise_area_spec_raises_for_missing_area(monkeypatch):
    monkeypatch.setattr(sweep, "Area", SimpleNamespace(nodes=_Nodes([])))
    with pytest.raises(LifecycleError, match="area .* not found"):
        await revise_area_spec(
            repository_uid="r1",
            area_uid="nonexistent-uid",
            instruction="make it shorter",
        )


# ── dispatch seams ───────────────────────────────────────────────────────────


@pytest.fixture
def dispatch_seams(monkeypatch):
    captured = {}

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
        return SimpleNamespace(uid="run42")

    monkeypatch.setattr(
        "domains.agents.services.composition.compose_agent_intent", fake_compose
    )
    monkeypatch.setattr(sweep, "trigger_run", fake_trigger)
    return captured


async def test_revise_area_spec_dispatches_one_ask_run(dispatch_seams, monkeypatch):
    area = _area("area-uid-1", key="features/auth", spec="old spec text", title="Auth Flow")
    monkeypatch.setattr(sweep, "Area", SimpleNamespace(nodes=_Nodes([area])))

    result = await revise_area_spec(
        repository_uid="r1",
        area_uid="area-uid-1",
        instruction="add rate limiting details",
        triggered_by="user-abc",
    )

    assert result.run_uid == "run42"
    assert result.errors == []

    compose = dispatch_seams["compose"]
    assert compose["agent_key"] == "revise-spec"
    assert compose["structural"] == _GENERATE_SPECS_TOOLING_CONTRACT
    # intent must carry both the instruction and the existing spec
    listing = compose["existing_state_listing"]
    assert "add rate limiting details" in listing
    assert "old spec text" in listing
    assert "features/auth" in listing

    trigger = dispatch_seams["trigger"]
    assert trigger["playbook"] == "ask"
    assert trigger["stage"] == "discover"
    assert trigger["trigger"] == RunTrigger.MANUAL
    assert "features/auth" in trigger["title"]
    assert trigger["triggered_by"] == "user-abc"


async def test_revise_area_spec_intent_contains_instruction_and_spec(dispatch_seams, monkeypatch):
    """Extra guard: both the instruction AND the current spec appear in the
    composed intent's existing_state_listing regardless of ordering."""
    area = _area(
        "area-uid-2",
        key="features/checkout",
        spec="checkout flow contract",
        scope_paths=["back_end/checkout"],
    )
    monkeypatch.setattr(sweep, "Area", SimpleNamespace(nodes=_Nodes([area])))

    await revise_area_spec(
        repository_uid="r1",
        area_uid="area-uid-2",
        instruction="include idempotency guarantees",
    )

    listing = dispatch_seams["compose"]["existing_state_listing"]
    assert "include idempotency guarantees" in listing
    assert "checkout flow contract" in listing


async def test_revise_area_spec_no_spec_area(dispatch_seams, monkeypatch):
    """An area with no existing spec uses the placeholder and still dispatches."""
    area = _area("area-uid-3", key="features/onboarding", spec="")
    monkeypatch.setattr(sweep, "Area", SimpleNamespace(nodes=_Nodes([area])))

    result = await revise_area_spec(
        repository_uid="r1",
        area_uid="area-uid-3",
        instruction="draft from scratch",
    )

    assert result.run_uid == "run42"
    listing = dispatch_seams["compose"]["existing_state_listing"]
    assert "(no spec yet)" in listing
    assert "draft from scratch" in listing


def test_revise_spec_result_dataclass():
    r = ReviseSpecResult()
    assert r.run_uid == ""
    assert r.errors == []
    assert r.summary == ""
