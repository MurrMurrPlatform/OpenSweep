"""G1 — OpenCode is a write-capable executor (the local-LLM delivery loop).

Three seams:
  1. `_provider_supports_write` accepts an opencode provider, so IMPLEMENT
     dispatch/resume is no longer rejected.
  2. `trigger_run` in a write mode resolves the write-capable executor from the
     provider (executor=None), re-selecting a write-capable one when the active
     provider is read-only, and refuses when none exists.
  3. The OpenCode adapter uses the write contract (edit + commit) instead of the
     investigate-only envelope when dispatched in IMPLEMENT mode.
"""

import pytest

from domains.executors import cli_tracking, prompt_kit
from domains.runs.schemas import Executor, ExecutionMode
from domains.runs.services import lifecycle


class _FakeProvider:
    def __init__(self, kind: str, uid: str = "p1", org_uid: str = "org1"):
        self.kind = kind
        self.uid = uid
        self.org_uid = org_uid
        self.enabled = True


def test_opencode_provider_supports_write():
    assert lifecycle._provider_supports_write(_FakeProvider("opencode")) is True
    # Read-only executors stay rejected.
    assert lifecycle._provider_supports_write(_FakeProvider("ollama")) is False
    assert lifecycle._provider_supports_write(_FakeProvider("codex_subscription")) is False  # removed kind → not write-capable


def test_opencode_in_write_capable_set():
    assert Executor.OPENCODE in lifecycle._WRITE_CAPABLE_EXECUTORS
    assert Executor.CLAUDE_CODE in lifecycle._WRITE_CAPABLE_EXECUTORS


def test_cli_tracking_write_prompt_is_a_write_contract():
    read = prompt_kit.system_prompt("cli_tracking")
    write = prompt_kit.system_prompt("cli_tracking_write")
    assert write != read
    # Write contract: commit in the sandbox, never push.
    assert "COMMIT" in write and "NEVER push" in write
    # Native platform surface still advertised (complete_run at minimum).
    assert "opensweep_platform_complete_run" in write
    # The read prompt keeps the investigate-only envelope contract.
    assert "envelope" in read.lower()


def test_opencode_adapter_selects_write_prompt_in_implement_mode():
    # The adapter's write branch keys on OPENCODE + IMPLEMENT; codex never trips
    # it (it is not write-capable and never reaches IMPLEMENT mode).
    assert cli_tracking._SYSTEM_PROMPT_WRITE != cli_tracking._SYSTEM_PROMPT
    assert "COMMIT" in cli_tracking._SYSTEM_PROMPT_WRITE


class _WriteReq:
    repository_uid = "r1"
    run_uid = "run1"
    intent = "do the thing"
    target: dict = {}
    context = ""
    policy = None
    effort = ""


def test_write_instruction_tells_agent_to_commit_not_envelope():
    req = _WriteReq()
    write = cli_tracking._write_instruction(req, 600)
    read = cli_tracking._instruction(req, 600)
    assert "COMMIT" in write and "opensweep_platform_complete_run" in write
    assert "Do NOT push" in write
    # The read instruction is investigate-only.
    assert "Investigate only" in read
    assert "Investigate only" not in write


async def test_select_write_capable_excludes_readonly_providers(monkeypatch):
    providers = [_FakeProvider("opencode", uid="oc"), _FakeProvider("ollama", uid="ol")]

    class _Nodes:
        async def filter(self, **_kw):
            return providers

    monkeypatch.setattr(lifecycle.LLMProvider, "nodes", _Nodes())

    captured: dict = {}

    async def _select(*, org_uid, exclude_uids=frozenset()):
        captured["exclude"] = set(exclude_uids)
        # select_provider would honour the exclusion; return the write-capable one.
        return _FakeProvider("opencode", uid="oc")

    monkeypatch.setattr(lifecycle, "select_provider", _select)

    chosen = await lifecycle._select_write_capable_provider("org1")
    assert chosen is not None and chosen.kind == "opencode"
    # Only the read-only (ollama) provider is excluded from the pick.
    assert captured["exclude"] == {"ol"}


async def test_select_write_capable_none_when_all_readonly(monkeypatch):
    providers = [_FakeProvider("ollama", uid="ol"), _FakeProvider("codex_subscription", uid="cx")]  # removed kinds

    class _Nodes:
        async def filter(self, **_kw):
            return providers

    monkeypatch.setattr(lifecycle.LLMProvider, "nodes", _Nodes())

    async def _select(*, org_uid, exclude_uids=frozenset()):
        # Both providers excluded → nothing selectable.
        return None

    monkeypatch.setattr(lifecycle, "select_provider", _select)

    assert await lifecycle._select_write_capable_provider("org1") is None
