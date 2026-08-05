"""submit_thread_plan tool: registration + validation surface (pure)."""

import contextlib
from importlib import import_module
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from domains.platform_tools.dispatcher import _TOOLS
from domains.platform_tools.submit_thread_plan import _validate, submit_thread_plan


def test_tool_is_registered_in_dispatcher():
    assert "submit_thread_plan" in _TOOLS


def test_validation_rejects_empty_plan():
    with pytest.raises(HTTPException) as exc:
        _validate(thread_uid="th-1", plan_markdown="   ")
    assert exc.value.status_code == 422


def test_validation_rejects_missing_thread_uid():
    with pytest.raises(HTTPException):
        _validate(thread_uid="", plan_markdown="## Plan")


# ── write-race lock (concurrent drafts on the same thread) ───────────────────


class _Saveable(SimpleNamespace):
    async def save(self):
        return self


@pytest.mark.asyncio
async def test_submit_thread_plan_serializes_on_resolved_thread_uid(monkeypatch):
    """The plan write must serialize on the RESOLVED thread uid — the tool
    self-heals a ticket-uid candidate into the active thread, and two calls
    (one with the ticket uid, one with the real thread uid) must contend on
    the same lock so their timeline events cannot race."""
    mod = import_module("domains.platform_tools.submit_thread_plan")
    thread = _Saveable(
        uid="th-real",
        phase="refining",
        plan_text="",
        plan_state="",
        events=[],
        subject_ticket_uid="tk-1",
        repository_uid="repo-1",
        updated_at=None,
    )

    async def fake_resolve(candidate, run_uid=""):
        # Candidate can be the ticket uid ("tk-1") — resolver returns the real
        # thread. This is exactly the collision the lock has to defeat.
        return thread

    class _FakeNodes:
        async def get_or_none(self, uid):
            # In-lock re-fetch: always the same node.
            return thread

    async def fake_mirror(_):
        return None

    async def fake_audit(**_):
        return None

    captured: dict = {}

    @contextlib.asynccontextmanager
    async def fake_lock(key):
        captured["lock_key"] = key
        yield True

    monkeypatch.setattr(mod, "dist_lock", fake_lock)
    monkeypatch.setattr(mod, "write_audit", fake_audit)

    # Patch the lazily-imported symbols inside the tool function.
    from domains.threads import models as thread_models
    from domains.threads.services import thread_service

    monkeypatch.setattr(thread_models.Thread, "nodes", _FakeNodes())
    monkeypatch.setattr(thread_service, "resolve_thread", fake_resolve)
    monkeypatch.setattr(thread_service, "mirror_plan_to_ticket", fake_mirror)

    result = await submit_thread_plan(
        thread_uid="tk-1",  # ticket uid on purpose — self-heals to th-real
        plan_markdown="## Plan\nDo the thing.",
        run_uid="run-1",
        executor="claude_code",
    )
    assert captured["lock_key"] == "thread:plan:th-real"
    assert result == {"thread_uid": "th-real", "plan_state": "drafted"}
    assert thread.plan_text.startswith("## Plan")
    assert thread.plan_state == "drafted"
    assert any(e.get("type") == "plan_drafted" for e in thread.events)


@pytest.mark.asyncio
async def test_submit_thread_plan_rejects_non_refining_after_lock(monkeypatch):
    """The in-lock re-fetch must respect a phase change that happened while
    we waited (e.g. a concurrent thread transition flipped it to implementing)."""
    mod = import_module("domains.platform_tools.submit_thread_plan")
    stale = _Saveable(
        uid="th-1",
        phase="refining",  # what the first resolve saw
        plan_text="",
        plan_state="",
        events=[],
        subject_ticket_uid="tk-1",
        repository_uid="repo-1",
        updated_at=None,
    )
    fresh = _Saveable(**{**stale.__dict__, "phase": "implementing"})

    async def fake_resolve(candidate, run_uid=""):
        return stale

    class _FakeNodes:
        async def get_or_none(self, uid):
            return fresh  # someone transitioned the thread while we waited

    @contextlib.asynccontextmanager
    async def fake_lock(key):
        yield True

    async def fake_audit(**_):
        return None

    monkeypatch.setattr(mod, "dist_lock", fake_lock)
    monkeypatch.setattr(mod, "write_audit", fake_audit)
    from domains.threads import models as thread_models
    from domains.threads.services import thread_service

    monkeypatch.setattr(thread_models.Thread, "nodes", _FakeNodes())
    monkeypatch.setattr(thread_service, "resolve_thread", fake_resolve)

    with pytest.raises(HTTPException) as exc:
        await submit_thread_plan(
            thread_uid="th-1",
            plan_markdown="## Plan",
        )
    assert exc.value.status_code == 409
