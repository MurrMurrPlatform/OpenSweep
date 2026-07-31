"""An epic member is never approved or dispatched on its own.

WHY: `df4261f` made an epic ONE run and ONE PR against the parent — the
members are injected into the parent's work list and the merged PR closes
every one of them. `EpicService.approve` therefore leaves member statuses
alone on purpose ("a member is never dispatched against directly and never
needs to pass Gate 1 on its own").

Nothing enforced that, though. A member could still be walked through Gate 1
by hand, and `trigger_implement_run` gated only on status — so an approved
member could be dispatched individually, opening a SECOND branch over the
same files and racing the epic's own PR to merge. Whichever merged first
would close the ticket while the other kept building against it.

WHAT: Gate 1 refuses a ticket with a parent, and the dispatch path refuses
it too. Both are needed: the gate covers approve-then-implement, and the
dispatch check covers a ticket that was already `todo` when it was grouped
(approval does not reset member statuses, so this is the common case).
DB-free in-memory fakes, mirroring test_epic_approval.py.
"""

import pytest
from fastapi import HTTPException

import domains.tickets.services.ticket_service as ticket_mod
from domains.tickets.services.ticket_service import TicketService

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


class FakeTicket(_Node):
    class nodes:
        @staticmethod
        async def get_or_none(**kw):
            for n in _STORE.get("FakeTicket", []):
                if all(getattr(n, k, None) == v for k, v in kw.items()):
                    return n
            return None

        @staticmethod
        async def filter(**kw):
            return [
                n
                for n in _STORE.get("FakeTicket", [])
                if all(getattr(n, k, None) == v for k, v in kw.items())
            ]


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    _STORE.clear()
    _AUDITS.clear()
    monkeypatch.setattr(ticket_mod, "Ticket", FakeTicket)

    async def audit(**kw):
        _AUDITS.append(kw)

    monkeypatch.setattr(ticket_mod, "write_audit", audit)
    yield
    _STORE.clear()
    _AUDITS.clear()


def _ticket(uid: str, *, status="backlog", parent="") -> FakeTicket:
    t = FakeTicket(
        uid=uid,
        repository_uid="r1",
        title=f"ticket {uid}",
        status=status,
        parent_ticket_uid=parent,
        priority="medium",
        labels=[],
        acceptance_criteria=[],
        description="",
        updated_at=None,
        created_at=None,
        done_at=None,
        approved_by="",
        approved_at=None,
    )
    _STORE.setdefault("FakeTicket", []).append(t)
    return t


# ── Gate 1 ───────────────────────────────────────────────────────────────


async def test_gate_1_refuses_an_epic_member():
    _ticket("child", parent="epic-1")

    with pytest.raises(HTTPException) as exc:
        await TicketService().transition(
            "child", "todo", actor_uid="u1", actor_role="maintainer"
        )

    assert exc.value.status_code == 409
    assert "epic-1" in str(exc.value.detail), "point the reviewer at the parent"


async def test_refused_member_is_left_untouched():
    """A refused gate must not half-apply — no status move, no approval stamp."""
    child = _ticket("child", parent="epic-1")

    with pytest.raises(HTTPException):
        await TicketService().transition(
            "child", "todo", actor_uid="u1", actor_role="maintainer"
        )

    assert child.status == "backlog"
    assert not child.approved_by
    assert child.approved_at is None
    assert not _AUDITS, "a refused transition must not be audited as one"


async def test_gate_1_still_passes_for_an_ungrouped_ticket():
    """The guard keys on the parent link, not on being a ticket."""
    solo = _ticket("solo")

    await TicketService().transition("solo", "todo", actor_uid="u1", actor_role="maintainer")

    assert solo.status == "todo"
    assert solo.approved_by == "u1"


async def test_gate_1_passes_for_the_epic_parent():
    """The parent has children but no parent of its own — it IS the gate."""
    parent = _ticket("epic-1")
    _ticket("child", parent="epic-1")

    await TicketService().transition("epic-1", "todo", actor_uid="u1", actor_role="maintainer")

    assert parent.status == "todo"


async def test_ungrouping_restores_the_gate():
    """Removing a ticket from its epic makes it independent work again."""
    child = _ticket("child", parent="epic-1")
    child.parent_ticket_uid = ""

    await TicketService().transition("child", "todo", actor_uid="u1", actor_role="maintainer")

    assert child.status == "todo"


async def test_non_gate_transitions_are_untouched_for_members():
    """Only Gate 1 is refused — a member grouped while in-progress must still
    be movable, or a bad state would be unrecoverable from the UI."""
    child = _ticket("child", status="in-progress", parent="epic-1")

    await TicketService().transition(
        "child", "in-review", actor_uid="u1", actor_role="maintainer"
    )

    assert child.status == "in-review"


# ── Dispatch ─────────────────────────────────────────────────────────────


async def test_implement_refuses_an_epic_member():
    """The case Gate 1 cannot catch: the ticket was approved BEFORE it was
    grouped, so it is sitting in `todo` with a parent."""
    import domains.delivery.services.implement_run_service as impl_mod

    member = _ticket("child", status="todo", parent="epic-1")

    with pytest.raises(HTTPException) as exc:
        await impl_mod.trigger_implement_run(member, triggered_by="u1")

    assert exc.value.status_code == 409
    assert "epic-1" in str(exc.value.detail)


async def test_implement_refuses_a_member_before_touching_the_repository(monkeypatch):
    """The refusal must land before `require_repository` — otherwise a member
    in a repo without GitHub reports a confusing 'no GitHub' error instead."""
    import domains.delivery.services.implement_run_service as impl_mod

    async def boom(*a, **kw):
        raise AssertionError("require_repository must not run for an epic member")

    monkeypatch.setattr(impl_mod, "require_repository", boom)
    member = _ticket("child", status="todo", parent="epic-1")

    with pytest.raises(HTTPException) as exc:
        await impl_mod.trigger_implement_run(member, triggered_by="u1")

    assert exc.value.status_code == 409
