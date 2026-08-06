"""Ticket c33ae2cf regression coverage — epics, state-machine, dedupe.

Verifies the concrete fixes bundled into
`Tickets domain: epics, state-machine correctness & semantic dedupe`:

- `epic_service.approve` emits `ticket.approved` on the Gate-1 backlog→todo
  move it performs directly (finding fbfd7522).
- `epic_service.bulk_approve` reports per-uid failure on transient DB errors
  instead of aborting the batch with a 500 (finding fafc51e2).
- `epics.builder.plan_epics` skips a failing draft, keeps going, and always
  returns `plan_uid` + per-draft errors so the batch's already-persisted
  proposals stay discoverable (finding fae3c190).
- `ticket_service.ungroup` / `remove_from_group` refuse to detach members
  once the epic has left backlog (finding c8da3e44).
- `ticket_service.update` refuses to re-parent a past-backlog ticket into
  an epic — the one-epic-one-PR invariant (memory 519b56f4).
- `ticket_service.validate_group_members` is backlog-only, not merely
  "not done" (finding 5ef7eacb).
- `ticket_service.create` refuses a near-duplicate open ticket with a
  structured 409 payload (minimal lexical layer for ticket 5570ff15).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import domains.tickets.services.epic_service as epic_mod
import domains.tickets.services.ticket_service as ticket_mod
from domains.tickets.schemas import CreateTicketRequest, UpdateTicketRequest
from domains.tickets.services.epic_service import EpicService
from domains.tickets.services.ticket_service import TicketService

pytestmark = pytest.mark.asyncio

_STORE: dict[str, list] = {}
_AUDITS: list[dict] = []


class _Node:
    # Neomodel fills in `default=""`/`default=[]` at __init__ time, so
    # production code accesses attributes without defensive guards. The fake
    # stores whatever it's given; missing attributes get an empty-string
    # fallback via __getattr__ so proposal_to_dto/ticket_to_dto don't AttrError.
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name):
        # Only reached when normal lookup fails. Datetime-shaped fields
        # need None so pydantic-model conversion accepts them.
        if name.startswith("_"):
            raise AttributeError(name)
        if name.endswith("_at"):
            return None
        return ""

    async def save(self):
        store = _STORE.setdefault(type(self).__name__, [])
        if self not in store:
            store.append(self)
        return self


def _nodes_for(key: str):
    class _Nodes:
        @staticmethod
        async def get_or_none(**kw):
            for n in _STORE.get(key, []):
                if all(getattr(n, k, None) == v for k, v in kw.items()):
                    return n
            return None

        @staticmethod
        async def filter(**kw):
            def _match(n):
                for k, v in kw.items():
                    if k.endswith("__in"):
                        real = k[: -len("__in")]
                        if getattr(n, real, None) not in v:
                            return False
                    elif getattr(n, k, None) != v:
                        return False
                return True

            return [n for n in _STORE.get(key, []) if _match(n)]

    return _Nodes


class FakeTicket(_Node):
    nodes = _nodes_for("FakeTicket")


class FakeProposal(_Node):
    nodes = _nodes_for("FakeProposal")


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    _STORE.clear()
    _AUDITS.clear()
    monkeypatch.setattr(ticket_mod, "Ticket", FakeTicket)
    monkeypatch.setattr(epic_mod, "Ticket", FakeTicket)
    monkeypatch.setattr(epic_mod, "EpicProposal", FakeProposal)
    # `find_open_ticket_duplicate` imports `Ticket` at call time — patch the
    # module reference the same way ticket_service is patched.
    import domains.tickets.models as models_mod

    monkeypatch.setattr(models_mod, "Ticket", FakeTicket)

    async def audit(**kw):
        _AUDITS.append(kw)

    monkeypatch.setattr(ticket_mod, "write_audit", audit)
    monkeypatch.setattr(epic_mod, "write_audit", audit)
    yield
    _STORE.clear()
    _AUDITS.clear()


def _ticket(uid: str, *, status="backlog", parent="", title=None, repo="r1") -> FakeTicket:
    t = FakeTicket(
        uid=uid,
        repository_uid=repo,
        title=title if title is not None else f"ticket {uid}",
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
        archived=False,
        archived_at=None,
        archived_by="",
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
        axis="root-cause",
        evidence={},
        plan_uid="plan1",
        origin="rule",
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


# ── ticket.approved audit on epic approval (finding fbfd7522) ────────────


async def test_epic_approve_emits_ticket_approved_audit_alongside_transitioned():
    """The manual Gate-1 path emits BOTH ticket.transitioned and ticket.approved.
    Any downstream keyed off ticket.approved (Gate-1 reports, notification
    triggers) would otherwise miss every ticket promoted through the epic path."""
    _ticket("t1")
    _ticket("t2")
    _proposal("p1", ["t1", "t2"])

    await EpicService().approve("p1", actor_uid="u1")

    kinds = [a["kind"] for a in _AUDITS]
    assert kinds.count("ticket.approved") == 1
    assert kinds.count("ticket.transitioned") == 1
    approved = next(a for a in _AUDITS if a["kind"] == "ticket.approved")
    assert approved["payload"]["approved_by"] == "u1"
    assert approved["payload"]["via"] == "epic_approval"


# ── bulk_approve tolerates non-HTTPException (finding fafc51e2) ─────────


async def test_bulk_approve_reports_db_error_as_partial_failure(monkeypatch):
    """A transient neomodel/Neo4j fault on ONE approval must not abort the
    whole batch — the caller loses partial-success visibility and any earlier
    approval is already committed."""
    _ticket("t1")
    _ticket("t2")
    _ticket("t3")
    _ticket("t4")
    _proposal("good", ["t1", "t2"])
    _proposal("boom", ["t3", "t4"])

    svc = EpicService()
    real_approve = svc.approve

    async def approve_maybe_boom(uid, *, actor_uid):
        if uid == "boom":
            raise RuntimeError("simulated Neo4j timeout")
        return await real_approve(uid, actor_uid=actor_uid)

    monkeypatch.setattr(svc, "approve", approve_maybe_boom)

    result = await svc.bulk_approve(["good", "boom"], actor_uid="u1")

    assert [p.uid for p in result["approved"]] == ["good"]
    assert [e["uid"] for e in result["errors"]] == ["boom"]
    assert "internal error: RuntimeError" in result["errors"][0]["detail"]


# ── plan_epics per-draft error handling (finding fae3c190) ──────────────


async def test_plan_epics_skips_stale_draft_and_returns_plan_uid_with_errors(monkeypatch):
    """A stale member in ONE draft must not orphan the batch's other
    proposals. The caller always gets its plan_uid and a per-draft error list."""
    from domains.tickets.schemas import PlanEpicsRequest
    from domains.tickets.services.epics import builder as builder_mod

    # Minimal drafts fixture: two epics, one with a member that will 404.
    class _Draft:
        def __init__(self, title, members):
            self.title = title
            self.rationale = "r"
            self.member_ticket_uids = members
            self.suggested_labels = []
            self.suggested_priority = "medium"
            from domains.tickets.services.epics.schemas import EpicAxis

            self.axis = EpicAxis.AREA
            self.evidence = {}

    async def fake_load(_repo):
        return []

    def fake_select(_facts, _sel):
        return []

    def fake_partition(_selected, _spec):
        return [_Draft("good", ["t1", "t2"]), _Draft("stale", ["nope", "gone"])]

    monkeypatch.setattr(builder_mod, "load_ticket_facts", fake_load)
    monkeypatch.setattr(builder_mod, "select_tickets", fake_select)
    monkeypatch.setattr(builder_mod, "partition", fake_partition)

    # Seed the two live tickets for the "good" draft; leave the "stale"
    # draft's members absent so validate_group_members raises 404.
    _ticket("t1")
    _ticket("t2")

    result = await builder_mod.plan_epics(
        PlanEpicsRequest(repository_uid="r1", axis="area"), actor_uid="u1"
    )

    assert result["plan_uid"], "plan_uid must be returned even on partial failure"
    assert len(result["epics"]) == 1
    assert result["epics"][0].member_ticket_uids == ["t1", "t2"]
    assert len(result["errors"]) == 1
    assert result["errors"][0]["title"] == "stale"


# ── ungroup / remove_from_group lifecycle guard (finding c8da3e44) ──────


async def test_ungroup_refuses_when_parent_is_past_backlog():
    """`mark_done_via_merge` iterates `parent_ticket_uid=uid` at merge time
    — a member cleared from that link mid-flight never advances to `done`."""
    _ticket("parent", status="in-progress")
    _ticket("m1", parent="parent")
    _ticket("m2", parent="parent")

    with pytest.raises(HTTPException) as exc:
        await TicketService().ungroup("parent", actor_uid="u1")
    assert exc.value.status_code == 409
    assert "past backlog" in str(exc.value.detail)


async def test_ungroup_still_works_when_parent_is_in_backlog():
    _ticket("parent", status="backlog")
    _ticket("m1", parent="parent")
    _ticket("m2", parent="parent")

    n = await TicketService().ungroup("parent", actor_uid="u1")

    assert n == 2
    # And the audit was written.
    assert any(a["kind"] == "epic.dissolved" for a in _AUDITS)


async def test_remove_from_group_refuses_when_parent_is_past_backlog():
    _ticket("parent", status="todo")
    child = _ticket("m1", parent="parent")

    with pytest.raises(HTTPException) as exc:
        await TicketService().remove_from_group("m1", actor_uid="u1")
    assert exc.value.status_code == 409
    assert child.parent_ticket_uid == "parent", "the detach must not half-apply"


async def test_remove_from_group_still_works_when_parent_is_in_backlog():
    _ticket("parent", status="backlog")
    child = _ticket("m1", parent="parent")

    await TicketService().remove_from_group("m1", actor_uid="u1")

    assert child.parent_ticket_uid == ""
    assert any(a["kind"] == "epic.member_removed" for a in _AUDITS)


# ── validate_group_members is backlog-only (finding 5ef7eacb) ───────────


async def test_validate_group_members_refuses_a_past_backlog_ticket():
    _ticket("t1", status="backlog")
    _ticket("t2", status="in-progress")

    with pytest.raises(HTTPException) as exc:
        await TicketService().validate_group_members("r1", ["t1", "t2"])
    assert exc.value.status_code == 409
    assert "backlog" in str(exc.value.detail)


async def test_epic_approve_drops_member_that_left_backlog_after_proposal():
    """A member that walked through Gate 1 between propose and approve is
    dropped like an already-claimed member — the batch does not 409 on
    natural staleness — and the drop is auditable."""
    _ticket("t1", status="backlog")
    _ticket("t2", status="in-progress")  # left backlog after proposal
    _ticket("t3", status="backlog")
    _proposal("p1", ["t1", "t2", "t3"])

    dto = await EpicService().approve("p1", actor_uid="u1")

    approved = next(a for a in _AUDITS if a["kind"] == "epic.approved")
    assert approved["payload"]["dropped_left_backlog"] == ["t2"]
    assert sorted(approved["payload"]["member_ticket_uids"]) == ["t1", "t3"]
    # And the parent was still created with the surviving members.
    parent_uid = dto.created_ticket_uid
    kids = [t.uid for t in await FakeTicket.nodes.filter(parent_ticket_uid=parent_uid)]
    assert sorted(kids) == ["t1", "t3"]


async def test_epic_approve_409_when_left_backlog_leaves_fewer_than_two():
    _ticket("t1", status="in-progress")
    _ticket("t2", status="in-review")
    _proposal("p1", ["t1", "t2"])

    with pytest.raises(HTTPException) as exc:
        await EpicService().approve("p1", actor_uid="u1")
    assert exc.value.status_code == 409
    assert "left backlog" in str(exc.value.detail)


# ── update() parent_ticket_uid bypass (memory 519b56f4) ─────────────────


async def test_update_refuses_reparenting_a_past_backlog_ticket():
    """`validate_group_members` refuses to group a past-backlog ticket, but
    the generic PATCH used to accept it — silently opening the same one-epic-
    one-PR hole."""
    _ticket("parent", status="backlog")
    _ticket("live", status="in-progress")

    with pytest.raises(HTTPException) as exc:
        await TicketService().update(
            "live",
            UpdateTicketRequest(parent_ticket_uid="parent"),
            actor_uid="u1",
        )
    assert exc.value.status_code == 409
    assert "backlog" in str(exc.value.detail)


async def test_update_still_reparents_a_backlog_ticket():
    _ticket("parent", status="backlog")
    child = _ticket("child", status="backlog")

    await TicketService().update(
        "child",
        UpdateTicketRequest(parent_ticket_uid="parent"),
        actor_uid="u1",
    )
    assert child.parent_ticket_uid == "parent"


# ── Lexical dedupe at ticket create (precursor to ticket 5570ff15) ──────


async def test_create_refuses_near_duplicate_open_ticket_by_default():
    """The primary reason this bundled ticket calls out dedupe: two agents
    proposing the same-titled ticket produced two tickets. The 409 payload
    names the existing ticket so the caller can update it or comment
    instead."""
    existing = _ticket("t1", title="Cypher label interpolation in audit.py")

    with pytest.raises(HTTPException) as exc:
        await TicketService().create(
            CreateTicketRequest(
                repository_uid="r1",
                title="Cypher label interpolation in audit.py",
            ),
            actor_uid="u1",
        )
    assert exc.value.status_code == 409
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["existing_ticket_uid"] == existing.uid


async def test_create_allows_near_duplicate_with_explicit_flag():
    _ticket("t1", title="Cypher label interpolation in audit.py")

    t = await TicketService().create(
        CreateTicketRequest(
            repository_uid="r1",
            title="Cypher label interpolation in audit.py",
            allow_duplicate=True,
        ),
        actor_uid="u1",
    )
    assert t.uid != "t1"
    assert t.title == "Cypher label interpolation in audit.py"


async def test_create_dedupe_scoped_to_repository():
    """Cross-repo title overlap must not block — tenancy has to hold."""
    _ticket("t1", title="Cypher label interpolation in audit.py", repo="other")

    t = await TicketService().create(
        CreateTicketRequest(
            repository_uid="r1",
            title="Cypher label interpolation in audit.py",
        ),
        actor_uid="u1",
    )
    assert t.repository_uid == "r1"


async def test_create_dedupe_ignores_done_and_archived_tickets():
    _ticket("done", status="done", title="Old resolved bug")
    arch = _ticket("arch", status="backlog", title="Old archived duplicate")
    arch.archived = True

    t = await TicketService().create(
        CreateTicketRequest(repository_uid="r1", title="Old resolved bug"),
        actor_uid="u1",
    )
    assert t.title == "Old resolved bug"


async def test_create_dedupe_skipped_for_finding_promotion(monkeypatch):
    """Finding-promoted tickets keep their existing bypass — the finding is
    the thing that was dedup'd; its promotion is a specific approved action."""
    _ticket("t1", title="Same title, unrelated origin")

    # Stub the finding-in-repo check to return a plausible finding-like object.
    class _Finding:
        uid = "f1"
        repository_uid = "r1"
        severity = ""
        kind = ""
        tags = []
        subtype = ""

    async def _has_finding(self, uid, repo):
        return _Finding()

    monkeypatch.setattr(
        ticket_mod.TicketService,
        "_require_finding_in_repo",
        _has_finding,
    )

    async def _noop(*args, **kw):
        return None

    # mark_ticketed is called at the end — stub the whole findings module import.
    import domains.findings.services.finding_service as fsvc

    monkeypatch.setattr(
        fsvc.FindingService, "mark_ticketed", _noop, raising=False
    )

    t = await TicketService().create(
        CreateTicketRequest(
            repository_uid="r1",
            title="Same title, unrelated origin",
            origin="finding",
            origin_finding_uid="f1",
        ),
        actor_uid="u1",
    )
    assert t.title == "Same title, unrelated origin"


async def test_create_epic_parent_bypasses_dedupe():
    """The epic parent's title often echoes its members' theme — a strict
    dedupe would 409 legitimate epic creation. Members enforce dedupe on
    themselves."""
    _ticket("m1")
    _ticket("m2")
    # Existing "same-title" ticket in the same repo — dedupe would normally block.
    _ticket("collision", title="backend/api · 2 tickets")

    parent = await TicketService().create_epic(
        repository_uid="r1",
        title="backend/api · 2 tickets",
        member_ticket_uids=["m1", "m2"],
        actor_uid="u1",
    )
    assert parent.title == "backend/api · 2 tickets"
    assert parent.uid != "collision"
