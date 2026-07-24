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


def _patch_session(monkeypatch, run_turn, *, acquire=None):
    """Patch the acquire → run → release seam cli_tracking now uses.

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
    monkeypatch.setattr(cli_tracking.codex_cli, "run_turn_on", run_turn)
    monkeypatch.setattr(cli_tracking.codex_cli, "release_app_server", _release)
    return released



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

    async def fake_run(session, *, instruction, working_dir, run_uid, model="", on_delta=None, timeout_s=None):
        invoked["app_server"] += 1
        if on_delta:
            on_delta("streamed ")
        return TurnResult(text="streamed answer", usage={"input_tokens": 3})

    released = _patch_session(monkeypatch, fake_run)
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

    async def fake_run(session, *, instruction, working_dir, run_uid, model="", on_delta=None, timeout_s=None):
        return TurnResult(text="ok", usage={})

    _patch_session(monkeypatch, fake_run)
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

    async def fail_run(session, *, instruction, working_dir, run_uid, model="", on_delta=None, timeout_s=None):
        raise RuntimeError("app-server connection refused")

    _patch_session(monkeypatch, fail_run)
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

    async def never_run(session, **kw):
        raise AssertionError("must not run a turn without the subscription")

    _patch_session(monkeypatch, never_run, acquire=busy_acquire)

    wrote = {"n": 0}
    monkeypatch.setattr(cli_tracking, "append_event", lambda *a, **k: wrote.__setitem__("n", wrote["n"] + 1))
    monkeypatch.setattr(cli_tracking, "record_input", lambda *a, **k: _async_none())

    result = await cli_tracking.CodexAdapter().dispatch(_req())

    assert result.status == RunStatus.PAUSED_QUOTA
    assert "busy" in (result.error or "").lower()
    # Nothing was written before the subscription was claimed, so the retry
    # doesn't duplicate the instruction in the transcript.
    assert wrote["n"] == 0
