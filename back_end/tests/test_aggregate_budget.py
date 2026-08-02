"""daily_repo_dollars must see spend reported by BOTH executor families.

Regression for a silent cap: policy_resolver summed usage["dollars"], but the
CLI executors (Claude Code / Codex — the entire write path) report spend as
usage["dollars_used"]. Only the internal LLM executor writes "dollars", so the
daily dollar ceiling never fired for the runs that actually cost money. These
assert the aggregate budget triggers on either key. DB-free (cypher mocked).
"""

import json
from types import SimpleNamespace

import pytest

import domains.run_policies.services.policy_resolver as pr_mod
from domains.run_policies.services.policy_resolver import (
    PolicyViolation,
    _check_aggregate_budgets,
)
from domains.runs.schemas import RunTrigger

pytestmark = pytest.mark.asyncio


def _policy(dollars: float):
    # Only the dollar ceiling is set, so only the dollars branch queries.
    return SimpleNamespace(
        daily_repo_run_count=0,
        daily_repo_wall_seconds=0,
        daily_repo_dollars=dollars,
    )


def _mock_cypher(monkeypatch, usage_blob: dict):
    async def fake_cypher_query(_q, _params):
        return [[json.dumps(usage_blob)]], None

    monkeypatch.setattr(pr_mod.adb, "cypher_query", fake_cypher_query)


@pytest.mark.parametrize("key", ["dollars", "dollars_used"])
async def test_autonomous_run_blocked_when_over_dollar_cap(monkeypatch, key):
    _mock_cypher(monkeypatch, {key: 12.0})
    with pytest.raises(PolicyViolation) as exc:
        await _check_aggregate_budgets(
            _policy(10.0),
            repository_uid="repo-1",
            trigger=RunTrigger.EVENT,  # autonomous → hard block
            warnings=[],
        )
    assert exc.value.code == "budget_dollars"


@pytest.mark.parametrize("key", ["dollars", "dollars_used"])
async def test_manual_run_warns_not_blocks(monkeypatch, key):
    _mock_cypher(monkeypatch, {key: 12.0})
    warnings: list[str] = []
    await _check_aggregate_budgets(
        _policy(10.0),
        repository_uid="repo-1",
        trigger=RunTrigger.MANUAL,  # human-triggered → warn only
        warnings=warnings,
    )
    assert any("daily_repo_dollars" in w for w in warnings)


async def test_under_cap_is_silent(monkeypatch):
    _mock_cypher(monkeypatch, {"dollars_used": 3.0})
    warnings: list[str] = []
    await _check_aggregate_budgets(
        _policy(10.0),
        repository_uid="repo-1",
        trigger=RunTrigger.EVENT,
        warnings=warnings,
    )
    assert warnings == []
