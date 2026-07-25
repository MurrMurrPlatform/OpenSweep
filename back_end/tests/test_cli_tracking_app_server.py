"""Route codex runs through the app-server when OPENSWEEP_CODEX_APP_SERVER=1.

The app-server path takes no PER-RUN credential lease — the persistent session
holds one lease for the server's whole lifetime, which is what allows many runs
on one subscription to run concurrently. This test file verifies:
  - flag ON  → the app-server seam is used, exec (invoke_provider) is NOT,
               assistant_text event appended, result finalized, session released.
  - no per-run lease on the app-server path (codex_credential_txn never entered).
  - flag OFF → exec path unchanged (per-run lease still taken).
  - subscription busy → resumable PAUSED_QUOTA, and nothing written to the
    transcript before the subscription was claimed.
"""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from domains.executors import cli_tracking
from domains.executors.base import DispatchRequest
from domains.runs.schemas import RunStatus

pytestmark = pytest.mark.asyncio


def _req(**overrides):
    base = dict(
        run_uid="r1",
        scheduled_agent_uid="a",
        repository_uid="repo1",
        repository_local_path="/ws",
        intent="ask",
    )
    base.update(overrides)
    return DispatchRequest(**base)


async def _async_none():
    return None


def _patch_session(monkeypatch, invoke, *, acquire=None):
    """Patch the acquire → invoke → release seam cli_tracking now uses.

    Without this the adapter would call the REAL registry, which takes a Neo4j
    credential lease and blocks — a hang, not a failure.
    """
    session = SimpleNamespace(uid="p1", client=None)
    released = {"n": 0}

    async def _acquire(provider):
        if acquire is not None:
            return await acquire(provider)
        return session

    async def _release(s):
        released["n"] += 1

    monkeypatch.setattr(cli_tracking.codex_cli, "acquire_app_server", _acquire)
    monkeypatch.setattr(cli_tracking.codex_cli, "invoke_via_app_server", invoke)
    monkeypatch.setattr(cli_tracking.codex_cli, "release_app_server", _release)
    return released


def _inv(text: str, **kw):
    """An LLMInvocation shaped exactly like the CLI transport returns."""
    from domains.llm_providers.services.llm_executor import LLMInvocation
    base = dict(raw_output=text, exit_code=0, transport="app-server")
    base.update(kw)
    return LLMInvocation(**base)



async def test_codex_dispatch_uses_app_server_when_enabled(monkeypatch):
    from domains.llm_providers.services.codex_app_server import TurnResult

    provider = SimpleNamespace(uid="p1", kind="codex_subscription", model="", credential_revision=0, extra_args="")

    async def _resolve(*a, **k):
        return provider

    monkeypatch.setattr(cli_tracking, "resolve_provider", _resolve)
    monkeypatch.setattr(cli_tracking.codex_cli, "app_server_enabled", lambda p: True)

    invoked = {"exec": 0, "app_server": 0, "events": []}

    async def boom_invoke(*a, **k):
        invoked["exec"] += 1
        raise AssertionError("exec path used")

    monkeypatch.setattr(cli_tracking, "invoke_provider", boom_invoke)

    async def fake_invoke(session, provider, *, system_prompt, instruction, timeout_seconds=None,
                          working_dir=None, on_chunk=None, run_uid=""):
        invoked["app_server"] += 1
        if on_chunk:
            await on_chunk("stdout", "streamed answer")
        return _inv("streamed answer")

    released = _patch_session(monkeypatch, fake_invoke)
    monkeypatch.setattr(cli_tracking, "append_event", lambda uid, kind, **kw: invoked["events"].append((kind, kw)))

    async def completed(uid):
        return True  # codex called complete_run via MCP

    monkeypatch.setattr(cli_tracking, "_completed_via_mcp", completed)
    monkeypatch.setattr(cli_tracking, "record_input", lambda *a, **k: _async_none())

    req = _req()
    result = await cli_tracking.CodexAdapter().dispatch(req)

    assert invoked["app_server"] == 1 and invoked["exec"] == 0
    assert released["n"] == 1, "the session must be handed back so the lease can release"
    assert any(k == "assistant_text" for k, _ in invoked["events"])
    assert result.status in (RunStatus.AWAITING_INPUT, RunStatus.RUNNING)  # finalized by lifecycle/_completed_via_mcp


async def test_app_server_path_skips_credential_lease(monkeypatch):
    """The app-server path must NOT enter codex_credential_txn.

    The persistent server owns the credential; taking the per-run lease would
    serialize all concurrent runs on one subscription — defeating the purpose.
    """
    from domains.llm_providers.services.codex_app_server import TurnResult

    provider = SimpleNamespace(uid="p1", kind="codex_subscription", model="", credential_revision=0, extra_args="")

    async def _resolve(*a, **k):
        return provider

    monkeypatch.setattr(cli_tracking, "resolve_provider", _resolve)
    monkeypatch.setattr(cli_tracking.codex_cli, "app_server_enabled", lambda p: True)

    lease_entered = {"entered": False}

    @asynccontextmanager
    async def _recording_txn(_provider):
        lease_entered["entered"] = True
        yield

    monkeypatch.setattr(cli_tracking.codex_credential, "codex_credential_txn", _recording_txn)

    async def fake_invoke(session, provider, *, system_prompt, instruction, timeout_seconds=None,
                          working_dir=None, on_chunk=None, run_uid=""):
        return _inv("ok")

    _patch_session(monkeypatch, fake_invoke)
    monkeypatch.setattr(cli_tracking, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(cli_tracking, "record_input", lambda *a, **k: _async_none())

    async def completed(uid):
        return True

    monkeypatch.setattr(cli_tracking, "_completed_via_mcp", completed)

    await cli_tracking.CodexAdapter().dispatch(_req())

    assert not lease_entered["entered"], "app-server path must NOT enter the credential lease"


async def test_app_server_error_returns_failed(monkeypatch):
    """An exception from run_via_app_server returns FAILED (not a crash)."""
    provider = SimpleNamespace(uid="p1", kind="codex_subscription", model="", credential_revision=0, extra_args="")

    async def _resolve(*a, **k):
        return provider

    monkeypatch.setattr(cli_tracking, "resolve_provider", _resolve)
    monkeypatch.setattr(cli_tracking.codex_cli, "app_server_enabled", lambda p: True)

    async def fail_invoke(session, provider, *, system_prompt, instruction, timeout_seconds=None,
                          working_dir=None, on_chunk=None, run_uid=""):
        return _inv("", exit_code=1, error="app-server: connection refused")

    _patch_session(monkeypatch, fail_invoke)
    monkeypatch.setattr(cli_tracking, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(cli_tracking, "record_input", lambda *a, **k: _async_none())

    result = await cli_tracking.CodexAdapter().dispatch(_req())

    assert result.status == RunStatus.FAILED
    assert "app-server" in (result.error or "").lower()


async def test_exec_path_unchanged_when_flag_off(monkeypatch):
    """flag OFF → exec path unchanged: lease still taken, run_via_app_server NOT called."""
    provider = SimpleNamespace(uid="p1", kind="codex_subscription", model="", credential_revision=0, extra_args="")

    async def _resolve(*a, **k):
        return provider

    monkeypatch.setattr(cli_tracking, "resolve_provider", _resolve)
    # Flag explicitly OFF
    monkeypatch.setattr(cli_tracking.codex_cli, "app_server_enabled", lambda p: False)

    app_server_called = {"called": False}

    async def should_not_call(*a, **k):
        app_server_called["called"] = True
        raise AssertionError("app-server should not be called when flag is off")

    monkeypatch.setattr(cli_tracking.codex_cli, "run_via_app_server", should_not_call)

    lease_entered = {"entered": False}

    @asynccontextmanager
    async def _recording_txn(_provider):
        lease_entered["entered"] = True
        yield

    monkeypatch.setattr(cli_tracking.codex_credential, "codex_credential_txn", _recording_txn)

    sentinel = object()

    async def _run_passes(self, req, prov, started):
        return sentinel

    monkeypatch.setattr(cli_tracking._CLITrackingAdapter, "_run_passes", _run_passes)

    result = await cli_tracking.CodexAdapter().dispatch(_req())

    assert not app_server_called["called"]
    assert lease_entered["entered"], "exec path must enter the credential lease"
    assert result is sentinel


async def test_lease_contention_pauses_for_retry_not_fail(monkeypatch):
    """Phase 4b: the app-server session holds the credential lease, so a process
    that cannot get it must pause the run for retry (resumable) — exactly like
    the exec path — instead of failing it."""
    from fastapi import HTTPException

    provider = SimpleNamespace(uid="p1", kind="codex_subscription", model="", credential_revision=0, extra_args="")

    async def _resolve(*a, **k):
        return provider

    monkeypatch.setattr(cli_tracking, "resolve_provider", _resolve)
    monkeypatch.setattr(cli_tracking.codex_cli, "app_server_enabled", lambda p: True)

    async def busy_acquire(provider):
        raise HTTPException(status_code=503, detail="Another run is using this Codex subscription")

    async def never_invoke(session, provider, **kw):
        raise AssertionError("must not run a turn without the subscription")

    _patch_session(monkeypatch, never_invoke, acquire=busy_acquire)

    wrote = {"n": 0}
    monkeypatch.setattr(cli_tracking, "append_event", lambda *a, **k: wrote.__setitem__("n", wrote["n"] + 1))
    monkeypatch.setattr(cli_tracking, "record_input", lambda *a, **k: _async_none())

    result = await cli_tracking.CodexAdapter().dispatch(_req())

    assert result.status == RunStatus.PAUSED_QUOTA
    assert "busy" in (result.error or "").lower()
    # Nothing was written before the subscription was claimed, so the retry
    # doesn't duplicate the instruction in the transcript.
    assert wrote["n"] == 0


# ── Transport parity ────────────────────────────────────────────────────────
#
# The bug this guards: the app-server used to be a PARALLEL pipeline that
# skipped _run_passes, so the agent's envelope tool_calls were never executed —
# runs completed having proposed nothing, and the raw envelope JSON leaked into
# the transcript. The app-server is a transport swap; a run must behave the same
# on either one.

_ENVELOPE = (
    'Here is my analysis.\n\n```json\n'
    '{"tool_calls": [{"tool": "propose_subsystem", "args": {"name": "auth"}},'
    ' {"tool": "complete_run", "args": {"summary": "done"}}],'
    ' "summary": "mapped the repo"}\n```'
)


async def _dispatch_on(monkeypatch, *, app_server: bool):
    """Run one dispatch on the chosen transport, returning (result, executed)."""
    provider = SimpleNamespace(uid="p1", kind="codex_subscription", model="",
                               credential_revision=0, extra_args="", org_uid="org-a")

    async def _resolve(*a, **k):
        return provider

    monkeypatch.setattr(cli_tracking, "resolve_provider", _resolve)
    monkeypatch.setattr(cli_tracking.codex_cli, "app_server_enabled", lambda p: app_server)
    monkeypatch.setattr(cli_tracking, "append_event", lambda *a, **k: None)
    monkeypatch.setattr(cli_tracking, "record_input", lambda *a, **k: _async_none())
    monkeypatch.setattr(cli_tracking.artifact_store, "put", lambda **kw: "artifact://raw")

    @asynccontextmanager
    async def _txn(_p):
        yield

    monkeypatch.setattr(cli_tracking.codex_credential, "codex_credential_txn", _txn)

    executed = {"calls": None}

    async def _exec_calls(*, calls, req, executor_value, deny_tools):
        executed["calls"] = calls
        return ([{"tool": c["tool"], "ok": True} for c in calls], ["artifact://tool"], {"ok": True})

    monkeypatch.setattr(cli_tracking, "execute_envelope_tool_calls", _exec_calls)

    async def _completed(uid):
        return True

    monkeypatch.setattr(cli_tracking, "_completed_via_mcp", _completed)

    if app_server:
        async def fake_invoke(session, prov, *, system_prompt, instruction, timeout_seconds=None,
                              working_dir=None, on_chunk=None, run_uid=""):
            if on_chunk:
                await on_chunk("stdout", _ENVELOPE)
            return _inv(_ENVELOPE)

        _patch_session(monkeypatch, fake_invoke)
    else:
        async def fake_cli(prov, **kw):
            on_chunk = kw.get("on_chunk")
            if on_chunk:
                # the exec transport streams codex JSONL, not plain text
                line = json.dumps({"type": "item.completed",
                                   "item": {"type": "agent_message", "text": _ENVELOPE}})
                await on_chunk("stdout", line + "\n")
            return _inv(_ENVELOPE, transport="cli")

        monkeypatch.setattr(cli_tracking, "invoke_provider", fake_cli)

    return await cli_tracking.CodexAdapter().dispatch(_req()), executed


async def test_app_server_executes_envelope_tool_calls(monkeypatch):
    """The regression itself: proposals in the envelope must actually be filed."""
    result, executed = await _dispatch_on(monkeypatch, app_server=True)

    assert executed["calls"] is not None, "envelope tool_calls were never executed"
    assert [c["tool"] for c in executed["calls"]] == ["propose_subsystem", "complete_run"]
    assert result.usage["tool_calls"] == 2
    assert result.parse_status == "ok"
    assert "artifact://tool" in result.output_refs


async def test_both_transports_produce_the_same_run(monkeypatch):
    """Same envelope in → same proposals, parse_status and outcome out."""
    app_result, app_exec = await _dispatch_on(monkeypatch, app_server=True)
    cli_result, cli_exec = await _dispatch_on(monkeypatch, app_server=False)

    assert app_exec["calls"] == cli_exec["calls"]
    assert app_result.status == cli_result.status
    assert app_result.parse_status == cli_result.parse_status
    assert app_result.outcome == cli_result.outcome
    assert app_result.usage["tool_calls"] == cli_result.usage["tool_calls"]
    assert app_result.output_refs == cli_result.output_refs
    # The only thing that should differ is which transport ran it.
    assert app_result.usage["transport"] == "app-server"
    assert cli_result.usage["transport"] == "cli"
