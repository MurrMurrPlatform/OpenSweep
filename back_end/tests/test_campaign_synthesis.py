"""The synthesis part — a campaign that reports on itself.

`build_summary` produces a tally: counts by severity, coverage rows, holes.
Nobody ever read the findings AS A SET and answered what a maintainer
actually asks — what is worst, what is systemic, what to do first, and where
the audit itself fell short. The one run type that DOES produce that (a
deep-scan Analysis) was manual-only and disconnected from campaigns.
"""

from types import SimpleNamespace

import pytest

from domains.campaigns.services import part_dispatch
from domains.campaigns.services.planner import build_plan_by_kind
from domains.campaigns.services.tick import plan_tick


def _lens(key="bugs", global_agent_key=""):
    return {"key": key, "global_agent_key": global_agent_key, "enabled": True}


def _area(key):
    return {
        "area_key": key,
        "title": key,
        "scope_paths": [key],
        "doc_uids": [],
        "file_count": 10,
    }


# ── Planning ─────────────────────────────────────────────────────────────


def test_synthesis_is_appended_last():
    parts = build_plan_by_kind(
        "subsystem", [_area("a"), _area("b")], [_lens()], synthesis=True
    )
    assert [p["kind"] for p in parts] == ["area", "area", "synthesis"]
    # Indices stay contiguous — the tick keys everything on them.
    assert [p["idx"] for p in parts] == [0, 1, 2]


def test_an_empty_plan_gets_no_synthesis():
    """There is no honest report to write about zero work, and a lone
    synthesis part would make an empty campaign look like it did something."""
    assert build_plan_by_kind("subsystem", [], [_lens()], synthesis=True) == []


def test_synthesis_is_opt_in():
    parts = build_plan_by_kind("subsystem", [_area("a")], [_lens()])
    assert [p["kind"] for p in parts] == ["area"]


# ── Ordering ─────────────────────────────────────────────────────────────


def _part(idx, kind="area", state="pending", run_uid=""):
    return {"idx": idx, "kind": kind, "state": state, "run_uid": run_uid}


def test_synthesis_waits_for_the_area_parts():
    parts = [_part(0), _part(1, kind="synthesis")]
    assert plan_tick(parts, {}, max_parallel=5)["dispatch"] == [0]


def test_synthesis_waits_for_the_global_sweeps_too():
    """It reports on everything the campaign produced, so it can only be
    written once there is nothing left to add."""
    parts = [
        _part(0, state="done"),
        _part(1, kind="global"),
        _part(2, kind="synthesis"),
    ]
    assert plan_tick(parts, {}, max_parallel=5)["dispatch"] == [1]


def test_synthesis_dispatches_once_everything_else_is_terminal():
    parts = [
        _part(0, state="done"),
        _part(1, kind="global", state="failed"),
        _part(2, kind="synthesis"),
    ]
    # A failed sweep still unlocks it — the report says so rather than
    # stranding the campaign.
    assert plan_tick(parts, {}, max_parallel=5)["dispatch"] == [2]


def test_an_unknown_part_kind_runs_in_the_first_phase():
    """A new kind must not silently jump the queue ahead of the sweeps."""
    parts = [_part(0, kind="something-new"), _part(1, kind="global")]
    assert plan_tick(parts, {}, max_parallel=5)["dispatch"] == [0]


# ── The prompt ───────────────────────────────────────────────────────────


def test_the_digest_block_carries_real_coverage_not_assumed_coverage():
    summary = {
        "counts": {"total": 4, "by_severity": {"high": 1, "low": 3}},
        "coverage": {
            "parts": [
                {
                    "idx": 0,
                    "title": "backend",
                    "state": "done",
                    "covered": 40,
                    "observed": 6,
                    "coverage_source": "observed",
                }
            ],
            "lens_rollup": [
                {
                    "lens": "performance",
                    "checked-clean": 0,
                    "checked-findings": 0,
                    "skipped": 2,
                    "missing": 1,
                }
            ],
            "holes": ["src/untouched"],
        },
    }

    block = part_dispatch._digest_block(summary)

    assert "findings: 4" in block
    # Claimed vs measured, both shown — the gap is the point.
    assert "40 claimed / 6 measured" in block
    assert "performance" in block and "2 skipped" in block
    assert "src/untouched" in block


def test_the_contract_forbids_re_auditing_and_demands_stated_limitations():
    contract = part_dispatch._SYNTHESIS_CONTRACT
    assert "NOT to audit the" in contract and "repository again" in contract
    assert "Do not file new findings" in contract
    # The failure mode this exists to prevent: a confident report over an
    # audit that never covered half the repo.
    assert "limitations" in contract
    assert "did NOT cover" in contract


@pytest.mark.asyncio
async def test_dispatch_composes_the_analysis_authoring_agent(monkeypatch):
    captured = {}

    async def fake_compute_digest(campaign):
        return {"counts": {"total": 1}}, []

    async def fake_compose(**kwargs):
        captured["compose"] = kwargs
        return SimpleNamespace(
            text="COMPOSED",
            agent_uid="a1",
            agent_rev=1,
            composed_degraded=False,
            degraded_layers=[],
        )

    async def fake_policy(tier):
        return SimpleNamespace(uid="policy1")

    async def fake_trigger_run(**kwargs):
        captured["trigger"] = kwargs
        return SimpleNamespace(uid="run-synth")

    import domains.campaigns.services.finalize as finalize

    monkeypatch.setattr(finalize, "compute_digest", fake_compute_digest)
    monkeypatch.setattr(part_dispatch, "compose_agent_intent", fake_compose)
    monkeypatch.setattr(part_dispatch, "ensure_policy_for_effort", fake_policy)
    monkeypatch.setattr(part_dispatch, "trigger_run", fake_trigger_run)

    campaign = SimpleNamespace(
        uid="c1",
        repository_uid="repo1",
        title="Weekly audit",
        effort="",
        created_by="u1",
        trigger_provenance="manual",
        parts=[],
        summary={},
    )
    run_uid = await part_dispatch.dispatch_part(
        campaign, {"idx": 2, "kind": "synthesis", "title": "Synthesis"}
    )

    assert run_uid == "run-synth"
    # The Analysis-authoring base, not the plain audit agent.
    assert captured["compose"]["agent_key"] == "deep-scan"
    assert "upsert_analysis" in captured["compose"]["structural"]
    assert captured["trigger"]["target"]["synthesis"] is True
    assert captured["trigger"]["target"]["campaign_uid"] == "c1"
