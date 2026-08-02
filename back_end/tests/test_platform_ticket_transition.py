"""The agent-facing ticket transition tool is gated on repo agent autonomy.

Default: human-only (Gate 1). With Repository.agent_autonomy on, the agent
transitions with maintainer authority; the status matrix still applies. DB-free
— the ticket/service/gate are faked; the test pins the gate + actor_role.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api.v1.platform_tools_tickets as mod
from domains.users.schemas import UserDTO

pytestmark = pytest.mark.asyncio


class _Ticket:
    uid = "t1"
    repository_uid = "repo-a"


class _Nodes:
    async def get_or_none(self, uid):
        return _Ticket() if uid == "t1" else None


def _req(run_uid=""):
    headers = {"X-OpenSweep-Run-Uid": run_uid} if run_uid else {}
    return SimpleNamespace(headers=headers)


def _user():
    return UserDTO(uid="u", email="e@x.y", display_name="U", role="viewer", org_uid="org-a")


@pytest.fixture(autouse=True)
def wire(monkeypatch):
    monkeypatch.setattr(mod.Ticket, "nodes", _Nodes())

    async def ok_access(request, user, repo):
        return None

    monkeypatch.setattr(mod, "require_tool_repo_access", ok_access)
    monkeypatch.setattr(mod, "ticket_to_dto", lambda t: {"uid": t.uid, "status": t.status})


async def _set_autonomy(monkeypatch, enabled: bool):
    async def _flag(uid):
        return enabled

    monkeypatch.setattr(
        "domains.repositories.services.agent_capabilities.agent_autonomy_enabled", _flag
    )


async def test_transition_blocked_when_autonomy_off(monkeypatch):
    await _set_autonomy(monkeypatch, False)
    req = mod.PlatformTransitionTicketRequest(ticket_uid="t1", to_status="todo")
    with pytest.raises(HTTPException) as exc:
        await mod.platform_transition_ticket(req, _req(run_uid="run-1"), user=_user())
    assert exc.value.status_code == 403


async def test_transition_allowed_with_maintainer_authority_when_autonomy_on(monkeypatch):
    await _set_autonomy(monkeypatch, True)
    calls = {}

    class _Svc:
        async def transition(self, uid, to_status, *, actor_uid, actor_role):
            calls.update(uid=uid, to_status=to_status, actor_uid=actor_uid, actor_role=actor_role)
            return SimpleNamespace(uid=uid, status=to_status)

    monkeypatch.setattr(mod, "TicketService", _Svc)
    req = mod.PlatformTransitionTicketRequest(ticket_uid="t1", to_status="todo")
    dto = await mod.platform_transition_ticket(req, _req(run_uid="run-1"), user=_user())

    assert dto == {"uid": "t1", "status": "todo"}
    # Gate 1 passes because the agent acts with maintainer authority.
    assert calls["actor_role"] == "maintainer"
    # The run uid is the recorded actor when present.
    assert calls["actor_uid"] == "run-1"
    assert calls["to_status"] == "todo"


async def test_unknown_ticket_is_404(monkeypatch):
    await _set_autonomy(monkeypatch, True)
    req = mod.PlatformTransitionTicketRequest(ticket_uid="nope", to_status="todo")
    with pytest.raises(HTTPException) as exc:
        await mod.platform_transition_ticket(req, _req(), user=_user())
    assert exc.value.status_code == 404


# ── Epic proposal approval — same autonomy gate ──────────────────────────────


class _Proposal:
    repository_uid = "repo-a"
    status = "approved"
    created_ticket_uid = "epic-parent-1"


def _epic_service(monkeypatch, *, approve_ok=True):
    approved = {}

    class _Svc:
        async def get_node(self, uid):
            return _Proposal()

        async def approve(self, uid, *, actor_uid):
            if not approve_ok:
                raise AssertionError("approve must not be called when gated off")
            approved.update(uid=uid, actor_uid=actor_uid)
            return _Proposal()

    monkeypatch.setattr("domains.tickets.services.epic_service.EpicService", _Svc)
    return approved


async def test_epic_approve_blocked_when_autonomy_off(monkeypatch):
    await _set_autonomy(monkeypatch, False)
    _epic_service(monkeypatch, approve_ok=False)
    req = mod.PlatformApproveEpicRequest(proposal_uid="p1")
    with pytest.raises(HTTPException) as exc:
        await mod.platform_approve_epic(req, _req(run_uid="run-1"), user=_user())
    assert exc.value.status_code == 403


async def test_epic_approve_allowed_when_autonomy_on(monkeypatch):
    await _set_autonomy(monkeypatch, True)
    approved = _epic_service(monkeypatch, approve_ok=True)
    req = mod.PlatformApproveEpicRequest(proposal_uid="p1")
    out = await mod.platform_approve_epic(req, _req(run_uid="run-1"), user=_user())
    assert out["created_ticket_uid"] == "epic-parent-1"
    assert approved == {"uid": "p1", "actor_uid": "run-1"}
