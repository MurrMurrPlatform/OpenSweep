"""F4 (HIGH) — cross-org ticket references (parent + origin finding).

WHY: `TicketService.create`/`update` correctly scope the *new/edited* ticket's
repository, but the client-supplied `parent_ticket_uid` and `origin_finding_uid`
were trusted without checking they belong to the same repository. Reachable
from both the human API (POST /api/v1/tickets, PATCH /{uid}) and the platform
MCP tools (opensweep_platform_create_ticket/update_ticket). This let an org-A
maintainer or run-token agent:
  - parent a new ticket under another org's ticket (cross-tenant graph edge +
    an existence oracle for foreign uids — breaking the 404-not-403 rule), and
  - stamp a foreign finding uid as `origin_finding_uid`/`linked_finding_uids`.

WHAT: create/update must 404 when `parent_ticket_uid` or `origin_finding_uid`
resolves to a different repository (or does not exist), and still allow
same-repo references. DB-free with in-memory fakes.
"""

import pytest
from fastapi import HTTPException

import domains.tickets.services.ticket_service as svc_mod
from domains.tickets.schemas import CreateTicketRequest, UpdateTicketRequest
from domains.tickets.services.ticket_service import TicketService

pytestmark = pytest.mark.asyncio


class _Node:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    async def save(self):
        store = _STORE.setdefault(type(self).__name__, [])
        if self not in store:
            store.append(self)
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

        async def all(self):
            return list(_STORE.get(store_key, []))

    return _Nodes()


class FakeTicket(_Node):
    nodes = _nodes_for("FakeTicket")


class FakeFinding(_Node):
    nodes = _nodes_for("FakeFinding")


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    _STORE.clear()
    monkeypatch.setattr(svc_mod, "Ticket", FakeTicket)
    # Finding lookup is introduced by the fix; patch it if/when present.
    monkeypatch.setattr(svc_mod, "Finding", FakeFinding, raising=False)

    async def no_audit(**kw):
        pass

    monkeypatch.setattr(svc_mod, "write_audit", no_audit)
    yield
    _STORE.clear()


def _seed_ticket(uid, repo, status="backlog"):
    n = FakeTicket(uid=uid, repository_uid=repo, status=status)
    _STORE.setdefault("FakeTicket", []).append(n)
    return n


def _seed_finding(uid, repo):
    n = FakeFinding(uid=uid, repository_uid=repo)
    _STORE.setdefault("FakeFinding", []).append(n)
    return n


async def test_create_rejects_cross_org_parent():
    # New ticket in repo-a names a parent that lives in repo-b → 404.
    _seed_ticket("parent-b", "repo-b")
    req = CreateTicketRequest(title="child", repository_uid="repo-a", parent_ticket_uid="parent-b")
    with pytest.raises(HTTPException) as exc:
        await TicketService().create(req)
    assert exc.value.status_code == 404
    assert _STORE.get("FakeTicket", []) == [_STORE["FakeTicket"][0]]  # only the seed


async def test_create_rejects_cross_org_origin_finding():
    # origin_finding_uid pointing at another org's finding → 404.
    _seed_finding("f-b", "repo-b")
    req = CreateTicketRequest(title="t", repository_uid="repo-a", origin_finding_uid="f-b")
    with pytest.raises(HTTPException) as exc:
        await TicketService().create(req)
    assert exc.value.status_code == 404


async def test_create_allows_same_repo_references():
    _seed_ticket("parent-a", "repo-a")
    _seed_finding("f-a", "repo-a")
    req = CreateTicketRequest(
        title="child",
        repository_uid="repo-a",
        parent_ticket_uid="parent-a",
        origin_finding_uid="f-a",
    )
    t = await TicketService().create(req)
    assert t.repository_uid == "repo-a"
    assert t.parent_ticket_uid == "parent-a"
    assert t.origin_finding_uid == "f-a"


async def test_update_rejects_cross_org_reparent():
    # Re-parenting an existing ticket under a foreign-repo parent → 404.
    _seed_ticket("child-a", "repo-a")
    _seed_ticket("parent-b", "repo-b")
    with pytest.raises(HTTPException) as exc:
        await TicketService().update("child-a", UpdateTicketRequest(parent_ticket_uid="parent-b"))
    assert exc.value.status_code == 404


async def test_update_allows_same_repo_reparent():
    _seed_ticket("child-a", "repo-a")
    _seed_ticket("parent-a2", "repo-a")
    t = await TicketService().update("child-a", UpdateTicketRequest(parent_ticket_uid="parent-a2"))
    assert t.parent_ticket_uid == "parent-a2"
