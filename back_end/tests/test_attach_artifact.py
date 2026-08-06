"""attach_artifact: tenancy/path invariants (no DB).

The tool must never silently guess `repository_uid` from `target_uid` — that
segment is what the artifacts read route parses back out to org-check a
request, so a foreign target uid would embed a non-repo identifier in the
tenancy slot. The path segment under `<repo>/` must namespace non-run
targets so a ticket/finding uid cannot collide with a run uid in the same
repo. And the HTTP endpoint has to forward the RESOLVED repo uid to the
tool, not the raw request (agents rarely fill it in).
"""

from importlib import import_module

import pytest
from fastapi import HTTPException

from domains.platform_tools.attach_artifact import (
    _artifact_scope_segment,
    attach_artifact,
)

aa_mod = import_module("domains.platform_tools.attach_artifact")


def test_scope_segment_is_bare_run_uid_for_runs():
    # Runs keep the executor/lifecycle folder layout — a companion blob for a
    # run must land next to its raw transcript.
    assert _artifact_scope_segment("run", "run-abc") == "run-abc"


def test_scope_segment_namespaces_non_run_targets():
    # Prevents collision: a ticket uid and a run uid in the same repo would
    # otherwise share the same folder, and the artifacts read route cannot
    # distinguish them from the URI alone.
    assert _artifact_scope_segment("ticket", "abc123") == "ticket-abc123"
    assert _artifact_scope_segment("finding", "f9") == "finding-f9"
    assert _artifact_scope_segment("pull_request", "pr7") == "pull_request-pr7"


async def test_missing_repository_uid_is_422_not_silent_fallback(monkeypatch):
    async def _no_write(**_):
        raise AssertionError("audit should never fire when validation fails")

    monkeypatch.setattr(aa_mod, "write_audit", _no_write)
    with pytest.raises(HTTPException) as exc:
        await attach_artifact(
            target_uid="t1",
            target_type="ticket",
            artifact_type="log",
            content="hi",
            # repository_uid deliberately omitted
        )
    assert exc.value.status_code == 422
    assert "repository_uid is required" in exc.value.detail


async def test_blank_repository_uid_is_422(monkeypatch):
    async def _no_write(**_):
        raise AssertionError("audit should never fire when validation fails")

    monkeypatch.setattr(aa_mod, "write_audit", _no_write)
    with pytest.raises(HTTPException) as exc:
        await attach_artifact(
            target_uid="t1",
            target_type="ticket",
            artifact_type="log",
            content="hi",
            repository_uid="   ",
        )
    assert exc.value.status_code == 422


async def test_unknown_target_type_is_422(monkeypatch):
    async def _no_write(**_):
        raise AssertionError("audit should never fire when validation fails")

    monkeypatch.setattr(aa_mod, "write_audit", _no_write)
    with pytest.raises(HTTPException) as exc:
        await attach_artifact(
            target_uid="t1",
            target_type="spaceship",
            artifact_type="log",
            content="hi",
            repository_uid="repo-1",
        )
    assert exc.value.status_code == 422


async def test_stores_under_target_type_scoped_segment(monkeypatch):
    """The path second segment MUST be scoped by target_type for non-run
    targets so runs and other entities cannot collide inside a repo."""
    captured: dict = {}

    def fake_put(**kwargs):
        captured.update(kwargs)
        return "opensweep-artifact://repo-1/ticket-t1/log.txt"

    async def fake_audit(**kwargs):
        captured["audit"] = kwargs

    monkeypatch.setattr(aa_mod.artifact_store, "put", fake_put)
    monkeypatch.setattr(aa_mod, "write_audit", fake_audit)

    result = await attach_artifact(
        target_uid="t1",
        target_type="ticket",
        artifact_type="log",
        content="hi",
        repository_uid="repo-1",
        executor="claude_code",
    )
    assert captured["repository_uid"] == "repo-1"
    # The critical guarantee: the target_uid does NOT leak into the
    # repository-uid position (fallback bug) and does NOT sit bare in the
    # second segment (collision bug).
    assert captured["run_uid"] == "ticket-t1"
    assert captured["artifact_type"] == "log"
    assert result["target_uid"] == "t1"


async def test_run_target_stores_under_bare_run_uid(monkeypatch):
    captured: dict = {}

    def fake_put(**kwargs):
        captured.update(kwargs)
        return "opensweep-artifact://repo-1/run-9/raw.txt"

    async def fake_audit(**_):
        return None

    monkeypatch.setattr(aa_mod.artifact_store, "put", fake_put)
    monkeypatch.setattr(aa_mod, "write_audit", fake_audit)

    await attach_artifact(
        target_uid="run-9",
        target_type="run",
        artifact_type="raw",
        content="x",
        repository_uid="repo-1",
    )
    assert captured["run_uid"] == "run-9"


async def test_http_endpoint_forwards_resolved_repository_uid(monkeypatch):
    """The HTTP door resolves repository_uid from the target when the caller
    omits it. That resolved uid must reach the tool — otherwise the tool
    422s on the empty uid it sees in the raw request body."""
    from api.v1 import platform_tools as pt

    async def fake_resolver(target_uid, target_type):
        return "repo-resolved"

    async def fake_require(*_, **__):
        return None

    calls: list[dict] = []

    async def fake_tool(**kwargs):
        calls.append(kwargs)
        return {"target_uid": kwargs["target_uid"], "artifact_ref": "u"}

    monkeypatch.setattr(pt, "_artifact_target_repository_uid", fake_resolver)
    monkeypatch.setattr(pt, "require_tool_repo_access", fake_require)
    monkeypatch.setattr(pt, "attach_artifact", fake_tool)

    req = pt.AttachArtifactRequest(
        target_uid="tk-1",
        target_type="ticket",
        artifact_type="repro",
        content="hi",
        # repository_uid omitted — the raw request body has None here.
    )
    result = await pt.http_attach_artifact(req, request=object(), user=object())
    assert result["target_uid"] == "tk-1"
    assert calls, "the tool must be invoked"
    assert calls[0]["repository_uid"] == "repo-resolved"


async def test_http_endpoint_accepts_explicit_repository_uid_that_matches_owner(
    monkeypatch,
):
    """A caller-supplied repository_uid is an assertion to CHECK, not a
    shortcut that skips resolution. Matching the target's real owner is fine —
    but the resolver still runs."""
    from api.v1 import platform_tools as pt

    resolved: list[tuple] = []

    async def fake_resolver(target_uid, target_type):
        resolved.append((target_uid, target_type))
        return "repo-explicit"

    async def fake_require(*_, **__):
        return None

    calls: list[dict] = []

    async def fake_tool(**kwargs):
        calls.append(kwargs)
        return {"target_uid": kwargs["target_uid"], "artifact_ref": "u"}

    monkeypatch.setattr(pt, "_artifact_target_repository_uid", fake_resolver)
    monkeypatch.setattr(pt, "require_tool_repo_access", fake_require)
    monkeypatch.setattr(pt, "attach_artifact", fake_tool)

    req = pt.AttachArtifactRequest(
        target_uid="tk-1",
        target_type="ticket",
        artifact_type="repro",
        content="hi",
        repository_uid="repo-explicit",
    )
    await pt.http_attach_artifact(req, request=object(), user=object())
    assert resolved == [("tk-1", "ticket")], "the resolver must not be skipped"
    assert calls[0]["repository_uid"] == "repo-explicit"


async def test_http_endpoint_rejects_repository_uid_that_is_not_the_target_owner(
    monkeypatch,
):
    """The tenancy-bypass case: a caller in repo A naming a target owned by
    repo B. The HTTP door must refuse it exactly like the envelope door does,
    and must not invoke the tool."""
    from api.v1 import platform_tools as pt

    async def fake_resolver(target_uid, target_type):
        return "repo-b"

    async def fake_require(*_, **__):
        return None

    calls: list[dict] = []

    async def fake_tool(**kwargs):
        calls.append(kwargs)
        return {"target_uid": kwargs["target_uid"], "artifact_ref": "u"}

    monkeypatch.setattr(pt, "_artifact_target_repository_uid", fake_resolver)
    monkeypatch.setattr(pt, "require_tool_repo_access", fake_require)
    monkeypatch.setattr(pt, "attach_artifact", fake_tool)

    req = pt.AttachArtifactRequest(
        target_uid="finding-in-repo-b",
        target_type="finding",
        artifact_type="repro",
        content="hi",
        repository_uid="repo-a",
    )
    with pytest.raises(HTTPException) as exc:
        await pt.http_attach_artifact(req, request=object(), user=object())
    assert exc.value.status_code == 404
    assert not calls, "the tool must not run for a cross-tenant target"


async def test_audit_event_carries_repository_uid_and_pascal_case_label(monkeypatch):
    """The artifact.attached event is the only trail proving an agent attached
    output to an entity. Neo4j labels are case-sensitive PascalCase class
    names, so a lowercase subject_type would derive to no repository at all and
    file the event platform-level, invisible to the owning tenant."""
    captured: dict = {}

    def fake_put(**kwargs):
        return "artifacts/repo-1/ticket-tk-1/x.txt"

    async def fake_audit(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(aa_mod.artifact_store, "put", fake_put)
    monkeypatch.setattr(aa_mod, "write_audit", fake_audit)

    await attach_artifact(
        target_uid="tk-1",
        target_type="ticket",
        artifact_type="repro",
        content="x",
        repository_uid="repo-1",
    )
    assert captured["repository_uid"] == "repo-1"
    assert captured["subject_type"] == "Ticket"
    assert captured["subject_uid"] == "tk-1"


async def test_audit_label_map_covers_every_valid_target_type():
    """The two vocabularies must not drift: any target_type the tool accepts
    must have a graph label, or attach_artifact KeyErrors at the audit call."""
    assert set(aa_mod._AUDIT_LABEL) == aa_mod.VALID_TARGET_TYPES
