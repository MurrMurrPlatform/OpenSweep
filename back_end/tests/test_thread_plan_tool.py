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

    monkeypatch.setattr(mod, "write_audit", fake_audit)

    # Patch the lazily-imported symbols inside the tool function. The lock now
    # lives in thread_service so the human routes share it — patch it there.
    from domains.threads import models as thread_models
    from domains.threads.services import thread_service

    monkeypatch.setattr(thread_service, "dist_lock", fake_lock)
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

    monkeypatch.setattr(mod, "write_audit", fake_audit)
    from domains.threads import models as thread_models
    from domains.threads.services import thread_service

    monkeypatch.setattr(thread_service, "dist_lock", fake_lock)
    monkeypatch.setattr(thread_models.Thread, "nodes", _FakeNodes())
    monkeypatch.setattr(thread_service, "resolve_thread", fake_resolve)

    with pytest.raises(HTTPException) as exc:
        await submit_thread_plan(
            thread_uid="th-1",
            plan_markdown="## Plan",
        )
    assert exc.value.status_code == 409


# ── the human half of the same race ──────────────────────────────────────────


def test_human_and_agent_writers_share_one_lock_key():
    """The clobber this lock exists to stop is human↔agent, so both halves
    must derive the SAME key. Locking only the agent tool left a maintainer's
    PATCH /plan racing the agent's draft — neomodel save() writes every
    property, so the loser's plan_text vanished entirely."""
    from domains.threads.services.thread_service import thread_write_lock_key

    assert thread_write_lock_key("th-1") == "thread:plan:th-1"


@pytest.mark.asyncio
async def test_submit_for_review_takes_the_thread_write_lock(monkeypatch):
    """submit_for_review sets one flag but save() rewrites the whole node, so
    unlocked it clobbered a plan edit that landed after its read."""
    mod = import_module("domains.platform_tools.submit_for_review")
    thread = _Saveable(
        uid="th-real",
        phase="implementing",
        ready_for_review=False,
        events=[],
        updated_at=None,
    )

    async def fake_resolve(candidate, run_uid=""):
        return thread

    class _FakeNodes:
        async def get_or_none(self, uid):
            return thread

    captured: dict = {}

    @contextlib.asynccontextmanager
    async def fake_lock(key):
        captured["lock_key"] = key
        yield True

    async def fake_audit(**_):
        return None

    monkeypatch.setattr(mod, "write_audit", fake_audit)
    from domains.threads import models as thread_models
    from domains.threads.services import thread_service

    monkeypatch.setattr(thread_service, "dist_lock", fake_lock)
    monkeypatch.setattr(thread_models.Thread, "nodes", _FakeNodes())
    monkeypatch.setattr(thread_service, "resolve_thread", fake_resolve)

    result = await mod.submit_for_review(thread_uid="tk-1", executor="claude_code")
    assert captured["lock_key"] == "thread:plan:th-real"
    assert result["ready_for_review"] is True
    assert thread.ready_for_review is True
    assert [e["type"] for e in thread.events] == ["ready_for_review"]


@pytest.mark.asyncio
async def test_submit_for_review_rejects_phase_change_seen_only_after_lock(monkeypatch):
    """The in-lock re-fetch must respect a phase that moved while we waited —
    otherwise the gate is decided on the pre-lock read and is no gate at all."""
    mod = import_module("domains.platform_tools.submit_for_review")
    stale = _Saveable(
        uid="th-1",
        phase="implementing",  # what the first resolve saw
        ready_for_review=False,
        events=[],
        updated_at=None,
    )
    fresh = _Saveable(**{**stale.__dict__, "phase": "done"})

    async def fake_resolve(candidate, run_uid=""):
        return stale

    class _FakeNodes:
        async def get_or_none(self, uid):
            return fresh

    @contextlib.asynccontextmanager
    async def fake_lock(key):
        yield True

    async def fake_audit(**_):
        return None

    monkeypatch.setattr(mod, "write_audit", fake_audit)
    from domains.threads import models as thread_models
    from domains.threads.services import thread_service

    monkeypatch.setattr(thread_service, "dist_lock", fake_lock)
    monkeypatch.setattr(thread_models.Thread, "nodes", _FakeNodes())
    monkeypatch.setattr(thread_service, "resolve_thread", fake_resolve)

    with pytest.raises(HTTPException) as exc:
        await mod.submit_for_review(thread_uid="th-1")
    assert exc.value.status_code == 409
    assert stale.ready_for_review is False


@pytest.mark.asyncio
async def test_thread_write_lock_409s_rather_than_proceeding_unlocked(monkeypatch):
    """Timeout must surface as a retryable 409 — proceeding lockless is what
    drops the concurrent writer's fields in the first place."""
    from domains.threads.services import thread_service

    @contextlib.asynccontextmanager
    async def never_acquires(key):
        yield False

    monkeypatch.setattr(thread_service, "dist_lock", never_acquires)

    with pytest.raises(HTTPException) as exc:
        async with thread_service.thread_write_lock("th-1"):
            raise AssertionError("body must not run without the lock")
    assert exc.value.status_code == 409
