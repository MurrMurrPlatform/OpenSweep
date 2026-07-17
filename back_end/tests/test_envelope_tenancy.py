"""Envelope tool dispatch is tenancy-scoped (_shared.execute_envelope_tool_calls).

The envelope is model-emitted text: its args must never choose the target
repository (that would be a cross-org read/write primitive) nor forge run
provenance. Scope keys are FORCED to the run's own values, and by-uid tools
(update_finding / attach_artifact) must target an object inside the run's
repository — mirroring api/platform_scope for the HTTP surface.

Pure-Python: the dispatcher, run-event stream, and by-uid target resolver
are monkeypatched.
"""

import pytest

from domains.executors import _shared
from domains.executors._shared import execute_envelope_tool_calls
from domains.executors.base import DispatchRequest


def _req(repository_uid="repo-own", run_uid="run-1"):
    return DispatchRequest(
        run_uid=run_uid,
        investigation_uid="inv-1",
        repository_uid=repository_uid,
        repository_local_path=None,
        intent="x",
    )


@pytest.fixture()
def dispatched(monkeypatch):
    """Capture every dispatch_platform_tool call; silence run events."""
    calls: list[tuple[str, dict]] = []

    async def fake_dispatch(name, **kwargs):
        calls.append((name, kwargs))
        return {"ok": True}

    monkeypatch.setattr(_shared, "dispatch_platform_tool", fake_dispatch)
    monkeypatch.setattr(_shared, "append_event", lambda *a, **k: None)
    return calls


def _resolver(monkeypatch, repository_uid):
    """Stub the by-uid target resolver to a fixed owning repository."""

    async def fake_resolver(name, args):
        return repository_uid

    monkeypatch.setattr(_shared, "_envelope_target_repository_uid", fake_resolver)


class TestScopeKeysAreForced:
    async def test_model_supplied_repository_uid_is_overridden(self, dispatched):
        results, refs, outcome = await execute_envelope_tool_calls(
            calls=[
                {
                    "tool": "create_finding",
                    "args": {
                        "repository_uid": "repo-other-org",
                        "source_run_uid": "forged-run",
                        "executor": "forged",
                        "title": "t",
                    },
                }
            ],
            req=_req(),
            executor_value="codex",
        )
        assert len(dispatched) == 1
        name, kwargs = dispatched[0]
        assert name == "create_finding"
        assert kwargs["repository_uid"] == "repo-own"
        assert kwargs["source_run_uid"] == "run-1"
        assert kwargs["executor"] == "codex"
        assert kwargs["title"] == "t"
        assert results == [{"tool": "create_finding", "result": {"ok": True}}]

    async def test_scope_keys_injected_when_absent(self, dispatched):
        await execute_envelope_tool_calls(
            calls=[{"tool": "write_memory", "args": {"title": "m"}}],
            req=_req(),
            executor_value="opencode",
        )
        _, kwargs = dispatched[0]
        assert kwargs["repository_uid"] == "repo-own"
        assert kwargs["source_run_uid"] == "run-1"
        assert kwargs["executor"] == "opencode"

    async def test_read_tools_are_scoped_too(self, dispatched):
        await execute_envelope_tool_calls(
            calls=[{"tool": "read_doc", "args": {"repository_uid": "repo-b", "slug": "s"}}],
            req=_req(),
            executor_value="internal_llm",
        )
        _, kwargs = dispatched[0]
        assert kwargs["repository_uid"] == "repo-own"


class TestByUidToolsAreGated:
    async def test_update_finding_foreign_repo_is_rejected(self, dispatched, monkeypatch):
        _resolver(monkeypatch, "repo-other-org")
        results, _, _ = await execute_envelope_tool_calls(
            calls=[{"tool": "update_finding", "args": {"finding_uid": "f1", "changes": {}}}],
            req=_req(),
            executor_value="codex",
        )
        assert dispatched == []
        assert results == [{"tool": "update_finding", "error": "not found"}]

    async def test_update_finding_missing_target_is_rejected(self, dispatched, monkeypatch):
        _resolver(monkeypatch, "")
        results, _, _ = await execute_envelope_tool_calls(
            calls=[{"tool": "update_finding", "args": {"finding_uid": "ghost", "changes": {}}}],
            req=_req(),
            executor_value="codex",
        )
        assert dispatched == []
        assert results == [{"tool": "update_finding", "error": "not found"}]

    async def test_update_finding_own_repo_dispatches_without_scope_keys(
        self, dispatched, monkeypatch
    ):
        _resolver(monkeypatch, "repo-own")
        await execute_envelope_tool_calls(
            calls=[
                {
                    "tool": "update_finding",
                    "args": {"finding_uid": "f1", "changes": {"status": "fixed"}},
                }
            ],
            req=_req(),
            executor_value="codex",
        )
        assert len(dispatched) == 1
        _, kwargs = dispatched[0]
        # update_finding takes (finding_uid, changes, actor) — no scope keys.
        assert kwargs == {"finding_uid": "f1", "changes": {"status": "fixed"}}

    async def test_attach_artifact_foreign_target_is_rejected(self, dispatched, monkeypatch):
        _resolver(monkeypatch, "repo-other-org")
        results, _, _ = await execute_envelope_tool_calls(
            calls=[
                {
                    "tool": "attach_artifact",
                    "args": {"target_uid": "r9", "target_type": "run", "artifact_type": "log", "content": "x"},
                }
            ],
            req=_req(),
            executor_value="opencode",
        )
        assert dispatched == []
        assert results == [{"tool": "attach_artifact", "error": "not found"}]

    async def test_attach_artifact_own_repo_gets_forced_scope(self, dispatched, monkeypatch):
        _resolver(monkeypatch, "repo-own")
        await execute_envelope_tool_calls(
            calls=[
                {
                    "tool": "attach_artifact",
                    "args": {
                        "target_uid": "r1",
                        "target_type": "run",
                        "artifact_type": "log",
                        "content": "x",
                        "repository_uid": "repo-other-org",
                        "executor": "forged",
                    },
                }
            ],
            req=_req(),
            executor_value="opencode",
        )
        _, kwargs = dispatched[0]
        assert kwargs["repository_uid"] == "repo-own"
        assert kwargs["executor"] == "opencode"


class TestExistingContractsKept:
    async def test_complete_run_is_harvested_not_dispatched(self, dispatched):
        _, _, outcome = await execute_envelope_tool_calls(
            calls=[{"tool": "complete_run", "args": {"summary": "done"}}],
            req=_req(),
            executor_value="codex",
        )
        assert dispatched == []
        assert outcome.get("text") == "done"

    async def test_deny_tools_still_deny(self, dispatched):
        results, _, _ = await execute_envelope_tool_calls(
            calls=[{"tool": "attach_patch_to_finding", "args": {}}],
            req=_req(),
            executor_value="codex",
            deny_tools={"attach_patch_to_finding": "disabled"},
        )
        assert dispatched == []
        assert results == [{"tool": "attach_patch_to_finding", "error": "disabled"}]
