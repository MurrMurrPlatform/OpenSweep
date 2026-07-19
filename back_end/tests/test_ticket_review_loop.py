"""Ticket review loop wiring — findings land on the ticket, the thread's
ready signal exists, and the auto-fix draft guard exempts thread-owned PRs.

WHY: the review→fix loop was designed PR-scoped while the unified dev flow
opens DRAFT PRs, and review findings never reached the originating ticket:
1. `ResolutionService.ensure` must cross-link a bound finding onto the PR's
   ticket (`Ticket.linked_finding_uids` feeds the work-item view and future
   implement-run context) — best-effort, PR ledger stays authoritative.
2. The thread agent signals completion via `submit_for_review` (a flag, not
   a dispatch) — the tool must be registered on every surface and the go
   message must instruct the agent to call it.
3. `maybe_auto_fix_for_pr` skipped ALL drafts, which wedged the loop for
   thread-owned PRs (always draft until the ready signal un-drafts them).

DB-free: models are monkeypatched with in-memory fakes (same pattern as
test_resolution_tenancy.py).
"""

from types import SimpleNamespace

import pytest

import domains.delivery.services.fix_run_service as fix_mod
import domains.delivery.services.resolution_service as svc_mod
from domains.delivery.services.resolution_service import ResolutionService
from domains.threads.services.thread_run import build_go_message

pytestmark = pytest.mark.asyncio


class _Node:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    async def save(self):
        _STORE.setdefault(type(self).__name__, []).append(self)
        return self


_STORE: dict[str, list] = {}


def _nodes_for(store_key: str):
    class _Nodes:
        async def get_or_none(self, **kw):
            for n in _STORE.get(store_key, []):
                if all(getattr(n, k, None) == v for k, v in kw.items()):
                    return n
            return None

        async def filter(self, **kw):
            return [
                n
                for n in _STORE.get(store_key, [])
                if all(getattr(n, k, None) == v for k, v in kw.items())
            ]

    return _Nodes()


class FakeFinding(_Node):
    nodes = _nodes_for("FakeFinding")


class FakePullRequest(_Node):
    nodes = _nodes_for("FakePullRequest")


class FakeFindingResolution(_Node):
    nodes = _nodes_for("FakeFindingResolution")


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    _STORE.clear()
    monkeypatch.setattr(svc_mod, "Finding", FakeFinding)
    monkeypatch.setattr(svc_mod, "PullRequest", FakePullRequest)
    monkeypatch.setattr(svc_mod, "FindingResolution", FakeFindingResolution)

    async def no_audit(**kw):
        pass

    monkeypatch.setattr(svc_mod, "write_audit", no_audit)
    yield
    _STORE.clear()


def _seed_finding(uid, repo):
    n = FakeFinding(
        uid=uid, repository_uid=repo, status="open", evidence={}, severity="high", title="t"
    )
    _STORE.setdefault("FakeFinding", []).append(n)
    return n


def _seed_pr(uid, repo, **extra):
    n = FakePullRequest(uid=uid, repository_uid=repo, head_sha="abc123", **extra)
    _STORE.setdefault("FakePullRequest", []).append(n)
    return n


# ── 1. Finding → ticket cross-link on bind ──────────────────────────────────


async def test_ensure_links_finding_to_pr_ticket(monkeypatch):
    from domains.tickets.services.ticket_service import TicketService

    linked: list[tuple] = []

    async def fake_link(self, uid, finding_uid, *, actor_uid=None):
        linked.append((uid, finding_uid, actor_uid))

    monkeypatch.setattr(TicketService, "link_finding", fake_link)
    _seed_pr("pr-a", "repo-a", ticket_uid="tick-1")
    _seed_finding("f-a", "repo-a")
    await ResolutionService().ensure(finding_uid="f-a", pull_request_uid="pr-a")
    assert linked == [("tick-1", "f-a", "system")]


async def test_ensure_skips_ticket_link_without_ticket(monkeypatch):
    from domains.tickets.services.ticket_service import TicketService

    linked: list[tuple] = []

    async def fake_link(self, uid, finding_uid, *, actor_uid=None):
        linked.append((uid, finding_uid))

    monkeypatch.setattr(TicketService, "link_finding", fake_link)
    _seed_pr("pr-b", "repo-a")  # no ticket_uid
    _seed_finding("f-b", "repo-a")
    r = await ResolutionService().ensure(finding_uid="f-b", pull_request_uid="pr-b")
    assert r.finding_uid == "f-b"
    assert linked == []


async def test_ensure_survives_ticket_link_failure(monkeypatch):
    """The PR ledger is authoritative — a broken ticket link must not fail
    the bind."""
    from domains.tickets.services.ticket_service import TicketService

    async def broken_link(self, uid, finding_uid, *, actor_uid=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(TicketService, "link_finding", broken_link)
    _seed_pr("pr-c", "repo-a", ticket_uid="tick-9")
    _seed_finding("f-c", "repo-a")
    r = await ResolutionService().ensure(finding_uid="f-c", pull_request_uid="pr-c")
    assert r.finding_uid == "f-c"


# ── 2. Ready signal: tool registration + go-message contract ────────────────


async def test_submit_for_review_registered_everywhere():
    from domains.platform_tools import submit_for_review as pkg_export  # noqa: F401
    from domains.platform_tools.dispatcher import _TOOLS
    from mcp_app import OPENSWEEP_PLATFORM_TOOL_OPERATIONS

    assert "submit_for_review" in _TOOLS
    assert "opensweep_platform_submit_for_review" in OPENSWEEP_PLATFORM_TOOL_OPERATIONS


async def test_go_message_instructs_ready_signal():
    ticket = SimpleNamespace(uid="t-1", title="Fix labels")
    go = build_go_message(
        ticket=ticket,
        plan_state="none",
        plan_text="",
        work_branch="b",
        base_branch="main",
        denylist=[],
        children=None,
    )
    assert "opensweep_platform_submit_for_review" in go
    assert "t-1" in go


# ── 3. Auto-fix draft guard: thread-owned PRs stay in the loop ──────────────


def _wire_auto_fix(monkeypatch, pr, *, thread_run):
    """Patch every collaborator maybe_auto_fix_for_pr touches."""
    import domains.delivery.services.pull_request_service as pr_svc
    import domains.repositories.services.workflow as wf

    monkeypatch.setattr(fix_mod, "PullRequest", FakePullRequest)

    async def fake_stage_auto(repository_uid, stage):
        return True

    monkeypatch.setattr(wf, "stage_auto", fake_stage_auto)

    verdict = SimpleNamespace(result="request_changes", sha=pr.head_sha, verification_status="")

    async def fake_verdict(pr_uid, head_sha=""):
        return verdict

    monkeypatch.setattr(pr_svc, "latest_verdict_for", fake_verdict)

    async def fake_thread_conv(pr_arg):
        return thread_run

    monkeypatch.setattr(fix_mod, "_thread_conversation_for_pr", fake_thread_conv)

    dispatched: list = []

    async def fake_trigger(pr_arg, *, triggered_by="", trigger=None, finding_uids=None):
        dispatched.append(pr_arg.uid)
        return SimpleNamespace(uid="run-fix-1")

    monkeypatch.setattr(fix_mod, "trigger_fix_run", fake_trigger)
    return dispatched


async def test_auto_fix_skips_draft_without_thread(monkeypatch):
    pr = _seed_pr("pr-d", "repo-a", state="open", draft=True, github_number=5)
    dispatched = _wire_auto_fix(monkeypatch, pr, thread_run=None)
    run = await fix_mod.maybe_auto_fix_for_pr("pr-d")
    assert run is None
    assert dispatched == []


async def test_auto_fix_allows_draft_for_thread_owned_pr(monkeypatch):
    pr = _seed_pr("pr-e", "repo-a", state="open", draft=True, github_number=6)
    thread_run = SimpleNamespace(uid="run-thread-1", playbook="thread")
    dispatched = _wire_auto_fix(monkeypatch, pr, thread_run=thread_run)
    run = await fix_mod.maybe_auto_fix_for_pr("pr-e")
    assert run is not None
    assert dispatched == ["pr-e"]


async def test_auto_fix_still_runs_for_ready_prs(monkeypatch):
    pr = _seed_pr("pr-f", "repo-a", state="open", draft=False, github_number=7)
    dispatched = _wire_auto_fix(monkeypatch, pr, thread_run=None)
    run = await fix_mod.maybe_auto_fix_for_pr("pr-f")
    assert run is not None
    assert dispatched == ["pr-f"]
