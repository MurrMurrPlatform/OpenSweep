"""The generalized single-continuation pass on the OpenCode adapter.

`opencode run` has no session resume, so a run that stops without finishing is
re-prompted ONCE with a capped transcript tail (the technique the codex
adapter pioneered; codex itself is gone). Write runs are excluded — the
nudges speak the envelope contract, which write runs don't use.
"""

import json
from types import SimpleNamespace

import pytest

from domains.executors import cli_tracking
from domains.executors.base import DispatchRequest
from domains.llm_providers.services.llm_executor import LLMInvocation
from domains.runs.schemas import ExecutionMode

pytestmark = pytest.mark.asyncio

_COMPLETE = {"tool": "complete_run", "args": {"summary": "done"}}


def _envelope_text(calls):
    return "text\n```json\n" + json.dumps({"tool_calls": calls, "summary": "s"}) + "\n```"


def _req(**overrides):
    base = dict(
        run_uid="r1", scheduled_agent_uid="", repository_uid="repo1",
        repository_local_path="/ws", intent="look around",
    )
    base.update(overrides)
    return DispatchRequest(**base)


async def _dispatch(monkeypatch, *, outputs, mode=None, exit_codes=None, policy=None):
    """Run the adapter against scripted CLI outputs; returns (result, prompts)."""
    provider = SimpleNamespace(uid="p1", kind="opencode", model="", extra_args="",
                               org_uid="org-a")

    async def _resolve(*a, **k):
        return provider

    async def _record(*a, **k):
        return None

    async def _exec_calls(*, calls, req, executor_value, deny_tools):
        return ([], [], {})

    async def _completed(uid):
        return False

    async def _no_gap(req, envelope):
        return ""

    monkeypatch.setattr(cli_tracking, "resolve_provider", _resolve)
    monkeypatch.setattr(cli_tracking, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(cli_tracking, "record_input", _record)
    monkeypatch.setattr(cli_tracking.artifact_store, "put", lambda **kw: "artifact://raw")
    monkeypatch.setattr(cli_tracking, "execute_envelope_tool_calls", _exec_calls)
    monkeypatch.setattr(cli_tracking, "_completed_via_mcp", _completed)
    monkeypatch.setattr(cli_tracking, "area_partition_nudge", _no_gap)

    prompts: list[str] = []
    codes = list(exit_codes or [0] * len(outputs))

    async def fake_cli(prov, **kw):
        prompts.append(kw["instruction"])
        i = len(prompts) - 1
        code = codes[i]
        return LLMInvocation(
            raw_output=outputs[i], exit_code=code, transport="cli",
            error="" if code == 0 else f"CLI exited {code}",
        )

    monkeypatch.setattr(cli_tracking, "invoke_provider", fake_cli)

    req = _req() if mode is None else _req(mode=mode)
    if policy is not None:
        req.policy = policy
    result = await cli_tracking.OpenCodeAdapter().dispatch(req)
    return result, prompts


async def test_incomplete_run_gets_one_continuation_pass(monkeypatch):
    result, prompts = await _dispatch(
        monkeypatch,
        outputs=[
            _envelope_text([{"tool": "create_finding", "args": {}}]),  # no complete_run
            _envelope_text([_COMPLETE]),
        ],
    )
    assert len(prompts) == 2
    assert "no session resume" in prompts[1]
    assert result.usage["continuation_pass"] is True
    assert result.usage["continuation_reason"] == "incomplete_run"


async def test_continuation_prompt_carries_the_transcript_tail(monkeypatch):
    first = _envelope_text([{"tool": "create_finding", "args": {"marker": "tail-marker"}}])
    _, prompts = await _dispatch(monkeypatch, outputs=[first, _envelope_text([_COMPLETE])])
    assert "tail-marker" in prompts[1]


async def test_finished_run_is_not_reprompted(monkeypatch):
    result, prompts = await _dispatch(monkeypatch, outputs=[_envelope_text([_COMPLETE])])
    assert len(prompts) == 1
    assert result.usage["continuation_pass"] is False


async def test_crashed_first_pass_is_never_reprompted(monkeypatch):
    result, prompts = await _dispatch(
        monkeypatch, outputs=["boom"], exit_codes=[2],
    )
    assert len(prompts) == 1
    assert result.usage["continuation_pass"] is False


async def test_write_runs_are_excluded_from_continuation(monkeypatch):
    # A write run reports via native MCP tools, not the envelope — the
    # envelope-speaking nudge must never fire on it.
    result, prompts = await _dispatch(
        monkeypatch, outputs=["edited files, committed"], mode=ExecutionMode.IMPLEMENT,
    )
    assert len(prompts) == 1
    assert result.usage["continuation_pass"] is False


async def test_policy_zero_passes_disables_continuation(monkeypatch):
    policy = SimpleNamespace(
        max_wall_seconds=None, max_tool_turns=None, max_files_touched=None,
        max_continuation_passes=0, warn_at_pct=80,
    )
    result, prompts = await _dispatch(
        monkeypatch,
        # An incomplete first pass that WOULD continue without the ban (see
        # test_incomplete_run_gets_one_continuation_pass).
        outputs=[_envelope_text([{"tool": "create_finding", "args": {}}])],
        policy=policy,
    )
    assert len(prompts) == 1
    assert result.usage["continuation_pass"] is False
