"""Epic approval: staleness handling and the cross-epic overlap guard.

WHY: `axes.partition` guarantees a ticket lands in at most one epic within
ONE plan, but nothing enforces that across the four producers (human, rule,
agent, schedule). Two live proposals could share a member — e.g. an `area`
rule plan and a `root-cause` agent plan both containing ticket X. Approving
both used to call `create_epic` twice, and the second call silently
overwrote `X.parent_ticket_uid`, moving X out of the epic approved first. It
vanished from that parent's children with no error, no 409, and no audit
record. Bulk approve made this much likelier by approving many at once.

WHAT: approval drops members already claimed by another epic (first approval
wins — the only outcome a reviewer can predict), 409s when that leaves fewer
than two, and records the drop in the audit payload. DB-free in-memory fakes.
"""

import pytest
from fastapi import HTTPException

import domains.tickets.services.epic_service as group_mod
import domains.tickets.services.ticket_service as ticket_mod
from domains.tickets.services.epic_service import EpicService

pytestmark = pytest.mark.asyncio

_STORE: dict[str, list] = {}
_AUDITS: list[dict] = []


class _Node:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    async def save(self):
        store = _STORE.setdefault(type(self).__name__, [])
        if self not in store:
            store.append(self)
        return self


def _nodes_for(key: str):
    class _Nodes:
        async def get_or_none(self, **kw):
            for n in _STORE.get(key, []):
                if all(getattr(n, k, None) == v for k, v in kw.items()):
                    return n
            return None

        async def filter(self, **kw):
            return [
                n
                for n in _STORE.get(key, [])
                if all(getattr(n, k, None) == v for k, v in kw.items())
            ]

        async def all(self):
            return list(_STORE.get(key, []))

    return _Nodes()


class FakeTicket(_Node):
    nodes = _nodes_for("FakeTicket")


class FakeProposal(_Node):
    nodes = _nodes_for("FakeProposal")


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    _STORE.clear()
    _AUDITS.clear()
    monkeypatch.setattr(group_mod, "Ticket", FakeTicket)
    monkeypatch.setattr(group_mod, "EpicProposal", FakeProposal)
    monkeypatch.setattr(ticket_mod, "Ticket", FakeTicket)

    async def audit(**kw):
        _AUDITS.append(kw)

    monkeypatch.setattr(group_mod, "write_audit", audit)
    monkeypatch.setattr(ticket_mod, "write_audit", audit)
    yield
    _STORE.clear()
    _AUDITS.clear()


def _ticket(uid: str, *, repo="r1", status="todo", parent="") -> FakeTicket:
    t = FakeTicket(
        uid=uid,
        repository_uid=repo,
        title=f"ticket {uid}",
        status=status,
        parent_ticket_uid=parent,
        priority="medium",
        labels=[],
        acceptance_criteria=[],
        description="",
        updated_at=None,
        created_at=None,
        approved_by="",
        approved_at=None,
    )
    _STORE.setdefault("FakeTicket", []).append(t)
    return t


def _proposal(uid: str, members: list[str], *, repo="r1") -> FakeProposal:
    p = FakeProposal(
        uid=uid,
        repository_uid=repo,
        title=f"epic {uid}",
        rationale="because",
        member_ticket_uids=members,
        suggested_labels=[],
        suggested_priority="medium",
        axis="files",
        evidence={},
        shape="single-pr",
        plan_uid="plan1",
        origin="rule",
        dispatch_state="",
        dispatched=[],
        max_parallel=3,
        status="proposed",
        source_run_uid="",
        created_ticket_uid="",
        reviewed_by="",
        reviewed_at=None,
        created_at=None,
        updated_at=None,
    )
    _STORE.setdefault("FakeProposal", []).append(p)
    return p


async def test_approval_creates_parent_and_passes_gate_1():
    """Approving the epic IS the Gate-1 approval — the reviewer must not have
    to find the new parent and approve it a second time."""
    _ticket("t1")
    _ticket("t2")
    _proposal("p1", ["t1", "t2"])

    dto = await EpicService().approve("p1", actor_uid="u1")

    parent = await FakeTicket.nodes.get_or_none(uid=dto.created_ticket_uid)
    assert parent is not None
    assert parent.status == "todo", "parent should not sit in backlog awaiting a 2nd gate"
    assert parent.approved_by == "u1"
    assert parent.approved_at is not None
    kinds = [a["kind"] for a in _AUDITS]
    assert "ticket.transitioned" in kinds, "Gate-1 move must stay audited"


async def test_member_already_in_another_epic_is_dropped_not_stolen():
    """The regression: t2 is already parented. Approving must leave it where
    it is rather than silently re-parenting it into this epic."""
    _ticket("t1")
    _ticket("t2", parent="other-parent")
    _ticket("t3")
    _proposal("p1", ["t1", "t2", "t3"])

    dto = await EpicService().approve("p1", actor_uid="u1")

    t2 = await FakeTicket.nodes.get_or_none(uid="t2")
    assert t2.parent_ticket_uid == "other-parent", "t2 was stolen from its first epic"

    parent_uid = dto.created_ticket_uid
    claimed = [
        t.uid
        for t in await FakeTicket.nodes.filter(parent_ticket_uid=parent_uid)
    ]
    assert sorted(claimed) == ["t1", "t3"]


async def test_dropped_members_are_recorded_in_the_audit():
    """A silent drop is still a drop — it has to be inspectable afterwards."""
    _ticket("t1")
    _ticket("t2", parent="other-parent")
    _ticket("t3")
    _proposal("p1", ["t1", "t2", "t3"])

    await EpicService().approve("p1", actor_uid="u1")

    approved = next(a for a in _AUDITS if a["kind"] == "epic.approved")
    assert approved["payload"]["dropped_already_grouped"] == ["t2"]
    assert "t2" not in approved["payload"]["member_ticket_uids"]


async def test_409_when_overlap_leaves_fewer_than_two_members():
    _ticket("t1", parent="other-parent")
    _ticket("t2", parent="other-parent")
    _ticket("t3")
    _proposal("p1", ["t1", "t2", "t3"])

    with pytest.raises(HTTPException) as exc:
        await EpicService().approve("p1", actor_uid="u1")
    assert exc.value.status_code == 409
    assert "already in another epic" in str(exc.value.detail)


async def test_done_and_foreign_members_are_still_dropped():
    """Pre-existing staleness rules must survive the overlap guard."""
    _ticket("t1")
    _ticket("t2", status="done")
    _ticket("t3", repo="other-repo")
    _ticket("t4")
    _proposal("p1", ["t1", "t2", "t3", "t4"])

    dto = await EpicService().approve("p1", actor_uid="u1")
    claimed = [
        t.uid
        for t in await FakeTicket.nodes.filter(parent_ticket_uid=dto.created_ticket_uid)
    ]
    assert sorted(claimed) == ["t1", "t4"]


async def test_bulk_approve_reports_partial_success():
    """One stale epic must not stop its siblings from landing, and the caller
    has to be told which failed — raising would make the outcome depend on
    iteration order."""
    _ticket("t1")
    _ticket("t2")
    _ticket("t3", parent="other-parent")
    _ticket("t4", parent="other-parent")
    _proposal("good", ["t1", "t2"])
    _proposal("stale", ["t3", "t4"])

    result = await EpicService().bulk_approve(
        ["good", "stale"], actor_uid="u1"
    )

    assert [p.uid for p in result["approved"]] == ["good"]
    assert [e["uid"] for e in result["errors"]] == ["stale"]
    assert "already in another epic" in result["errors"][0]["detail"]


async def test_bulk_approve_is_not_order_dependent():
    _ticket("t1")
    _ticket("t2")
    _ticket("t3", parent="other-parent")
    _ticket("t4", parent="other-parent")
    _proposal("good", ["t1", "t2"])
    _proposal("stale", ["t3", "t4"])

    result = await EpicService().bulk_approve(
        ["stale", "good"], actor_uid="u1"
    )
    assert [p.uid for p in result["approved"]] == ["good"]
    assert [e["uid"] for e in result["errors"]] == ["stale"]
