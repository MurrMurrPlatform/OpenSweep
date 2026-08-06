"""Regression pins for the delivery+threads concurrency/fire-and-forget bugs.

Three defects (memory 15f5e11 + ed1d0485) — one PR because the fixes share
`send_message_turn_and_wait`:

1. `ThreadService.attach_run` used the caller's stale Thread object for its
   save, so a `record_event` that appended between load and attach was
   silently clobbered (neomodel save() writes ALL declared properties).

2. `ThreadService._deliver_pending_answers` stamped `delivered_at` on
   pending answers BEFORE the fire-and-forget send. A send that failed
   after admission stranded the answers: the retry loop skips anything
   already stamped, so those answers evaporate forever.

3. `fix_run_service._message_fix_to_thread` burned `pr.fix_rounds` BEFORE
   the fire-and-forget send. The cold-dispatch path refunds on failure;
   this thread-message path did not, so a losing-race send stole a round.

All three fixes gate on `send_message_turn_and_wait` reporting admission
before committing the caller's state — the guarantee tested here.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import domains.threads.services.thread_run as tr
import domains.threads.services.thread_service as ts
from domains.delivery.services import fix_run_service as frs


# ── send_message_turn_and_wait: admission-signal contract ─────────────────


class _FakeTurnService:
    """Minimal TurnService stand-in — run_turn is an async generator whose
    behavior varies per test. The real TurnService yields its first event
    after the run's status guard + status flip; a raise before any yield
    means "rejected before admission".
    """

    def __init__(self, *, yields: list[dict] | None = None, raises: BaseException | None = None):
        self._yields = list(yields or [])
        self._raises = raises

    async def run_turn(self, uid, text):
        if self._raises is not None and not self._yields:
            raise self._raises
        for ev in self._yields:
            yield ev
        if self._raises is not None:
            raise self._raises


def _patch_turn(monkeypatch, service: _FakeTurnService) -> None:
    # thread_run imports TurnService locally inside _consume; patch the
    # module attribute it will read at that time.
    import domains.runs.services.turn_service as turn_service_mod

    class _Cls:
        def __init__(self):
            pass

        async def run_turn(self, uid, text):
            async for ev in service.run_turn(uid, text):
                yield ev

    monkeypatch.setattr(turn_service_mod, "TurnService", _Cls)


def test_send_and_wait_reports_admission_on_first_yield(monkeypatch):
    _patch_turn(monkeypatch, _FakeTurnService(yields=[{"type": "status", "status": "running"}]))
    result = asyncio.run(tr.send_message_turn_and_wait("r-1", "hello"))
    assert result is True


def test_send_and_wait_reports_rejection_when_turn_raises_before_yield(monkeypatch):
    _patch_turn(
        monkeypatch,
        _FakeTurnService(raises=HTTPException(status_code=409, detail="run is running")),
    )
    result = asyncio.run(tr.send_message_turn_and_wait("r-1", "hello"))
    assert result is False


def test_send_and_wait_reports_admission_when_turn_fails_after_yield(monkeypatch):
    _patch_turn(
        monkeypatch,
        _FakeTurnService(
            yields=[{"type": "status", "status": "running"}],
            raises=RuntimeError("subprocess died mid-turn"),
        ),
    )
    # The message was admitted (first yield reached); a later failure is
    # fire-and-forget business as usual and must not undo caller state.
    result = asyncio.run(tr.send_message_turn_and_wait("r-1", "hello"))
    assert result is True


# ── attach_run: reload-before-save discipline ────────────────────────────


class _FakeThread:
    def __init__(self, uid, *, phase="refining", events=None, run_uids=None, active=""):
        self.uid = uid
        self.phase = phase
        self.events = list(events or [])
        self.run_uids = list(run_uids or [])
        self.active_run_uid = active
        self.updated_at = None
        self.saved = 0

    async def save(self):
        self.saved += 1


def test_attach_run_reloads_before_save_never_clobbers_events(monkeypatch):
    # Caller's stale object has NO events; the fresh DB row has an event
    # written by a concurrent record_event between load and attach.
    stale = _FakeThread("th-1", events=[], run_uids=[])
    fresh = _FakeThread(
        "th-1",
        events=[{"type": "plan_edited"}],
        run_uids=[],
    )

    class _ThreadNodes:
        async def get_or_none(self, **kw):
            return fresh

    fake_run = SimpleNamespace(uid="run-1", thread_uid="", save=None)

    async def _run_save():
        fake_run.saved = True

    fake_run.save = _run_save

    class _RunNodes:
        async def get_or_none(self, **kw):
            return fake_run

    async def _no_op_record(thread, type, **payload):
        thread.events = [*(thread.events or []), {"type": type, **payload}]

    import domains.runs.models as run_models

    monkeypatch.setattr(ts, "Thread", SimpleNamespace(nodes=_ThreadNodes()))
    monkeypatch.setattr(run_models, "Run", SimpleNamespace(nodes=_RunNodes()))
    svc = ts.ThreadService()
    monkeypatch.setattr(svc, "record_event", _no_op_record)

    asyncio.run(svc.attach_run(stale, "run-1"))

    # The concurrent event must survive: attach_run saved the FRESH node,
    # not the stale one. A stale-object save would drop the plan_edited event.
    assert stale.saved == 0
    assert fresh.saved == 1
    assert any(e.get("type") == "plan_edited" for e in fresh.events)
    assert fresh.run_uids == ["run-1"]
    assert fresh.active_run_uid == "run-1"
    # Caller's view is refreshed so subsequent reads see reality.
    assert any(e.get("type") == "plan_edited" for e in stale.events)
    assert stale.run_uids == ["run-1"]


def test_attach_run_idempotent_branch_also_reloads(monkeypatch):
    # Already-attached run: only active_run_uid gets rewritten. Still must
    # save the fresh node so concurrent event appends are preserved.
    stale = _FakeThread("th-1", events=[], run_uids=["run-1"], active="")
    fresh = _FakeThread(
        "th-1",
        events=[{"type": "phase_changed"}],
        run_uids=["run-1"],
        active="",
    )

    class _ThreadNodes:
        async def get_or_none(self, **kw):
            return fresh

    monkeypatch.setattr(ts, "Thread", SimpleNamespace(nodes=_ThreadNodes()))
    svc = ts.ThreadService()

    async def _fail_record(thread, type, **payload):
        raise AssertionError("record_event must NOT run on the idempotent branch")

    monkeypatch.setattr(svc, "record_event", _fail_record)

    asyncio.run(svc.attach_run(stale, "run-1"))

    assert stale.saved == 0
    assert fresh.saved == 1
    assert fresh.active_run_uid == "run-1"
    assert any(e.get("type") == "phase_changed" for e in fresh.events)


# ── _deliver_pending_answers: admission-gated stamp ──────────────────────


def _pending_thread(uid="th-1"):
    return _FakeThread(
        uid,
        events=[
            {"type": "question", "uid": "q-1", "status": "answered", "answer": "yes"},
        ],
        active="run-1",
    )


def test_deliver_pending_stamps_only_after_admission(monkeypatch):
    thread = _pending_thread()

    async def _accepts(_run_uid):
        return True

    async def _admitted(_run_uid, _text):
        return True

    class _ThreadNodes:
        async def get_or_none(self, **kw):
            return thread

    monkeypatch.setattr(tr, "run_accepts_message", _accepts)
    monkeypatch.setattr(tr, "send_message_turn_and_wait", _admitted)
    monkeypatch.setattr(ts, "Thread", SimpleNamespace(nodes=_ThreadNodes()))

    svc = ts.ThreadService()
    ok = asyncio.run(svc._deliver_pending_answers(thread))
    assert ok is True
    q = next(e for e in thread.events if e.get("uid") == "q-1")
    assert q.get("delivered_at")  # stamp landed after admission


def test_deliver_pending_leaves_answers_unstamped_when_send_rejected(monkeypatch):
    """The critical fix: a rejected send must NOT strand the answers as
    "delivered but lost" — the retry loop skips anything with delivered_at
    stamped, so a false stamp evaporates the answer forever."""
    thread = _pending_thread()

    async def _accepts(_run_uid):
        return True  # pre-check passes; race happens inside send

    async def _rejected(_run_uid, _text):
        return False

    class _ThreadNodes:
        async def get_or_none(self, **kw):
            return thread

    monkeypatch.setattr(tr, "run_accepts_message", _accepts)
    monkeypatch.setattr(tr, "send_message_turn_and_wait", _rejected)
    monkeypatch.setattr(ts, "Thread", SimpleNamespace(nodes=_ThreadNodes()))

    svc = ts.ThreadService()
    ok = asyncio.run(svc._deliver_pending_answers(thread))
    assert ok is False
    q = next(e for e in thread.events if e.get("uid") == "q-1")
    assert not q.get("delivered_at")  # retry loop can pick it up next turn
    # No save landed on either the caller or the fresh node.
    assert thread.saved == 0


# ── _message_fix_to_thread: admission-gated round burn ────────────────────


class _FakePR:
    def __init__(self, uid="pr-1", fix_rounds=0):
        self.uid = uid
        self.repository_uid = "repo-1"
        self.github_number = 7
        self.pr_key = "repo-1:7"
        self.title = "Test PR"
        self.head_ref = "opensweep/x"
        self.base_ref = "main"
        self.state = "open"
        self.fix_rounds = fix_rounds
        self.fix_rounds_exhausted = False
        self.updated_at = None
        self.saved = 0

    async def save(self):
        self.saved += 1


class _FakeRun:
    def __init__(self, uid="run-1"):
        self.uid = uid


def _stub_frs_env(monkeypatch, *, accepts=True, admitted=True):
    async def _accepts(_uid):
        return accepts

    async def _admitted(_uid, _text):
        return admitted

    async def _policy(_repo):
        return SimpleNamespace(max_fix_rounds=5)

    async def _collect(pr, policy, uids):
        return [
            {
                "resolution_uid": "res-1",
                "finding_uid": "f-1",
                "title": "Test finding",
                "severity": "medium",
                "tags": [],
                "blocking": True,
                "why_it_matters": "",
                "suggested_fix": "",
                "evidence": {},
                "affected_paths": [],
            }
        ]

    async def _note(_uid, _run):
        return None

    async def _audit(**_kw):
        return None

    import domains.threads.services.hooks as hooks_mod
    import domains.threads.services.thread_run as tr_mod

    monkeypatch.setattr(tr_mod, "run_accepts_message", _accepts)
    monkeypatch.setattr(tr_mod, "send_message_turn_and_wait", _admitted)
    monkeypatch.setattr(frs, "ensure_merge_policy", _policy)
    monkeypatch.setattr(frs, "_collect_fixable_findings", _collect)
    monkeypatch.setattr(frs, "write_audit", _audit)
    monkeypatch.setattr(hooks_mod, "note_fix_run_for_pr", _note)


def test_message_fix_to_thread_burns_round_after_admission(monkeypatch):
    _stub_frs_env(monkeypatch, accepts=True, admitted=True)
    pr = _FakePR(fix_rounds=1)
    run = _FakeRun()
    asyncio.run(frs._message_fix_to_thread(pr, run, None, triggered_by="test"))
    assert pr.fix_rounds == 2  # burned exactly once, AFTER admission
    assert pr.saved == 1


def test_message_fix_to_thread_never_burns_round_when_send_rejected(monkeypatch):
    """The critical fix: a fire-and-forget send that races with the turn's
    own guard must NOT irreversibly spend a fix round for a message the
    agent never received."""
    _stub_frs_env(monkeypatch, accepts=True, admitted=False)
    pr = _FakePR(fix_rounds=1)
    run = _FakeRun()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(frs._message_fix_to_thread(pr, run, None, triggered_by="test"))
    assert exc.value.status_code == 409
    assert "NOT spent" in str(exc.value.detail)
    assert pr.fix_rounds == 1  # unchanged
    assert pr.saved == 0  # never saved the burn


# ── start_implement: admission-gated phase flip ──────────────────────────


def test_start_implement_admits_message_before_phase_flip(monkeypatch):
    """start_implement previously fired the GO message fire-and-forget AFTER
    flipping phase to 'implementing'. A rejected send left the thread stuck
    in that phase with an uninformed agent. Fix: await admission first."""

    class _Ticket:
        uid = "tk-1"
        title = "Do a thing"
        status = "in-progress"
        repository_uid = "repo-1"
        acceptance_criteria: list[str] = []

    class _Run:
        uid = "run-1"
        playbook = "thread"
        target: dict = {"work_branch": "b", "base_branch": "main"}

    thread = _FakeThread(
        "th-1",
        phase="refining",
        active="run-1",
        run_uids=["run-1"],
    )
    thread.plan_text = "plan"
    thread.plan_state = "approved"
    thread.subject_ticket_uid = "tk-1"
    thread.branch = "b"
    thread.repository_uid = "repo-1"

    class _ThreadNodes:
        async def get_or_none(self, **kw):
            return thread

    class _RunNodes:
        async def get_or_none(self, **kw):
            return _Run()

    class _TicketNodes:
        async def get_or_none(self, **kw):
            return _Ticket()

        async def filter(self, **kw):
            return []

    class _TicketSvc:
        async def get_node(self, uid):
            return _Ticket()

        async def transition(self, *a, **kw):
            return _Ticket()

    async def _accepts(_uid):
        return True

    async def _rejected(_uid, _text):
        return False

    async def _policy(_repo):
        return SimpleNamespace(max_fix_rounds=5, denylist=[])

    monkeypatch.setattr(ts, "Thread", SimpleNamespace(nodes=_ThreadNodes()))
    import domains.runs.models as run_models
    import domains.tickets.models as ticket_models
    import domains.tickets.services.ticket_service as ticket_svc_mod

    monkeypatch.setattr(run_models, "Run", SimpleNamespace(nodes=_RunNodes()))
    monkeypatch.setattr(ticket_models, "Ticket", SimpleNamespace(nodes=_TicketNodes()))
    monkeypatch.setattr(ticket_svc_mod, "TicketService", _TicketSvc)
    import domains.delivery.services.resolution_service as rs_mod

    monkeypatch.setattr(rs_mod, "ensure_merge_policy", _policy)
    monkeypatch.setattr(tr, "run_accepts_message", _accepts)
    monkeypatch.setattr(tr, "send_message_turn_and_wait", _rejected)

    svc = ts.ThreadService()

    async def _fail_transition(*a, **kw):
        raise AssertionError("phase must NOT flip when GO send is rejected")

    monkeypatch.setattr(svc, "transition", _fail_transition)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.start_implement("th-1", actor_uid="user-1"))
    assert exc.value.status_code == 409
    # Thread's phase is untouched — the guard fired before the transition.
    assert thread.phase == "refining"
