"""Ticket lifecycle — CRUD + Gate-1 state machine (PLATFORM_V2_DESIGN.md §2, §15 Phase 2).

Transition matrix (human API; every move audited):

    backlog ──► todo            GATE 1 — maintainer+ only; records approved_by/at
    todo ──► backlog            de-prioritize
    todo ──► in-progress
    in-progress ──► in-review
    in-progress ──► todo
    in-progress ──► backlog     de-prioritize
    in-review ──► in-progress
    in-review ──► done          sets done_at
    in-review ──► backlog       de-prioritize
    done ──► (terminal)

System moves (actor "system", audited, bypass the human matrix):
  - link-pr auto-advances todo/in-progress → in-review (work is under review)
  - a merged linked PR completes the ticket → done ("ticket.done_via_merge")
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from domains.findings.models import Finding
from domains.tickets.models import TICKET_ORIGINS, TICKET_PRIORITIES, Ticket
from domains.tickets.schemas import (
    CreateTicketRequest,
    TicketDetailDTO,
    TicketDTO,
    TicketStatus,
    UpdateTicketRequest,
)
from domains.users.schemas import role_at_least
from infrastructure.audit import write_audit

# Legal human transitions: {from: {to, ...}}. "Any → backlog except from done"
# plus the forward path with its two step-backs.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "backlog": frozenset({"todo"}),
    "todo": frozenset({"backlog", "in-progress"}),
    "in-progress": frozenset({"todo", "in-review", "backlog"}),
    "in-review": frozenset({"in-progress", "done", "backlog"}),
    "done": frozenset(),
}

GATE_1 = ("backlog", "todo")  # the human approval gate — maintainer+ only

_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2, "urgent": 3}


def is_legal_transition(from_status: str, to_status: str) -> bool:
    return to_status in LEGAL_TRANSITIONS.get(from_status, frozenset())


def priority_rank(priority: str) -> int:
    return _PRIORITY_RANK.get(priority, _PRIORITY_RANK["medium"])


def ticket_to_dto(t: Ticket) -> TicketDTO:
    return TicketDTO(
        uid=t.uid,
        repository_uid=t.repository_uid,
        title=t.title or "",
        description=t.description or "",
        acceptance_criteria=list(t.acceptance_criteria or []),
        labels=list(t.labels or []),
        status=TicketStatus(t.status or "backlog"),
        priority=t.priority or "medium",
        size=t.size or "",
        origin=t.origin or "human",
        origin_finding_uid=t.origin_finding_uid or "",
        parent_ticket_uid=t.parent_ticket_uid or "",
        linked_finding_uids=list(t.linked_finding_uids or []),
        linked_pr_uids=list(t.linked_pr_uids or []),
        assignee_uid=t.assignee_uid or "",
        approved_by=t.approved_by or "",
        approved_at=t.approved_at,
        done_at=t.done_at,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


class TicketService:
    async def get_node(self, uid: str) -> Ticket:
        t = await Ticket.nodes.get_or_none(uid=uid)
        if t is None:
            raise HTTPException(status_code=404, detail=f"Ticket {uid} not found")
        return t

    async def _require_ticket_in_repo(self, ticket_uid: str, repository_uid: str) -> None:
        """404 unless `ticket_uid` exists AND lives in `repository_uid` (F4)."""
        parent = await Ticket.nodes.get_or_none(uid=ticket_uid)
        if parent is None or parent.repository_uid != repository_uid:
            raise HTTPException(status_code=404, detail=f"Ticket {ticket_uid} not found")

    async def _require_finding_in_repo(self, finding_uid: str, repository_uid: str) -> None:
        """404 unless `finding_uid` exists AND lives in `repository_uid` (F4)."""
        finding = await Finding.nodes.get_or_none(uid=finding_uid)
        if finding is None or finding.repository_uid != repository_uid:
            raise HTTPException(status_code=404, detail=f"Finding {finding_uid} not found")

    async def list(
        self,
        *,
        repository_uid: str | None = None,
        status: str | None = None,
        origin: str | None = None,
        parent_ticket_uid: str | None = None,
        assignee_uid: str | None = None,
    ) -> list[TicketDTO]:
        filters: dict = {}
        if repository_uid:
            filters["repository_uid"] = repository_uid
        if status:
            filters["status"] = status
        if origin:
            filters["origin"] = origin
        if parent_ticket_uid:
            filters["parent_ticket_uid"] = parent_ticket_uid
        if assignee_uid:
            filters["assignee_uid"] = assignee_uid
        nodes = await (Ticket.nodes.filter(**filters) if filters else Ticket.nodes.all())
        out = [ticket_to_dto(t) for t in nodes]
        floor = datetime.min.replace(tzinfo=UTC)
        out.sort(
            key=lambda d: (priority_rank(d.priority), d.updated_at or d.created_at or floor),
            reverse=True,
        )
        return out

    async def get_detail(self, uid: str) -> TicketDetailDTO:
        t = await self.get_node(uid)
        children = await Ticket.nodes.filter(parent_ticket_uid=uid)
        child_dtos = [ticket_to_dto(c) for c in children]
        floor = datetime.min.replace(tzinfo=UTC)
        child_dtos.sort(
            key=lambda d: (priority_rank(d.priority), d.updated_at or d.created_at or floor),
            reverse=True,
        )
        return TicketDetailDTO(**ticket_to_dto(t).model_dump(), children=child_dtos)

    async def create(
        self, req: CreateTicketRequest, *, actor_uid: str | None = None
    ) -> Ticket:
        origin = req.origin or "human"
        if origin not in TICKET_ORIGINS:
            raise HTTPException(status_code=422, detail=f"invalid origin '{origin}'")
        # Tenancy (F4): parent_ticket_uid and origin_finding_uid are
        # client-supplied. The new ticket's own repo is gated at the route, but
        # these references must live in the SAME repository or they become
        # cross-org graph edges + an existence oracle for foreign uids. 404
        # (not 409) so a foreign uid never leaks its existence.
        if req.parent_ticket_uid:
            await self._require_ticket_in_repo(req.parent_ticket_uid, req.repository_uid)
        if req.origin_finding_uid:
            await self._require_finding_in_repo(req.origin_finding_uid, req.repository_uid)
        t = Ticket(
            uid=uuid4().hex,
            repository_uid=req.repository_uid,
            title=req.title,
            description=req.description,
            acceptance_criteria=req.acceptance_criteria,
            labels=req.labels,
            priority=req.priority or "medium",
            size=req.size,
            origin=origin,
            origin_finding_uid=req.origin_finding_uid,
            linked_finding_uids=[req.origin_finding_uid] if req.origin_finding_uid else [],
            parent_ticket_uid=req.parent_ticket_uid,
            assignee_uid=req.assignee_uid,
        )
        await t.save()
        await write_audit(
            kind="ticket.created",
            subject_uid=t.uid,
            subject_type="Ticket",
            actor_uid=actor_uid,
            payload={"repository_uid": t.repository_uid, "origin": origin, "title": t.title},
        )
        return t

    async def update(
        self, uid: str, req: UpdateTicketRequest, *, actor_uid: str | None = None
    ) -> Ticket:
        t = await self.get_node(uid)
        changes = req.model_dump(exclude_none=True)
        if "parent_ticket_uid" in changes and changes["parent_ticket_uid"]:
            if changes["parent_ticket_uid"] == uid:
                raise HTTPException(status_code=422, detail="a ticket cannot be its own parent")
            # Tenancy (F4): a re-parent may only target a ticket in the SAME
            # repository as the ticket being edited.
            await self._require_ticket_in_repo(changes["parent_ticket_uid"], t.repository_uid)
        for field, value in changes.items():
            setattr(t, field, value)
        t.updated_at = datetime.now(UTC)
        await t.save()
        await write_audit(
            kind="ticket.updated",
            subject_uid=t.uid,
            subject_type="Ticket",
            actor_uid=actor_uid,
            payload={"fields": sorted(changes.keys())},
        )
        return t

    # ── Grouping (batch related tickets under one parent) ────────────────

    async def validate_group_members(
        self, repository_uid: str, member_ticket_uids: list[str]
    ) -> list[Ticket]:
        """Resolve + validate a grouping's member set: ≥2 unique tickets,
        all in `repository_uid`, none done. Order-preserving."""
        uids = list(dict.fromkeys(u for u in (member_ticket_uids or []) if u))
        if len(uids) < 2:
            raise HTTPException(
                status_code=422, detail="a group needs at least 2 distinct member tickets"
            )
        members: list[Ticket] = []
        for uid in uids:
            t = await Ticket.nodes.get_or_none(uid=uid)
            if t is None:
                raise HTTPException(status_code=404, detail=f"Ticket {uid} not found")
            if t.repository_uid != repository_uid:
                raise HTTPException(
                    status_code=409, detail=f"Ticket {uid} belongs to another repository"
                )
            if t.status == "done":
                raise HTTPException(
                    status_code=409, detail=f"Ticket {uid} is done — nothing left to batch"
                )
            members.append(t)
        return members

    async def group_tickets(
        self,
        *,
        repository_uid: str,
        title: str,
        description: str = "",
        member_ticket_uids: list[str],
        labels: list[str] | None = None,
        priority: str = "medium",
        origin: str = "human",
        actor_uid: str | None = None,
    ) -> Ticket:
        """Create a parent ticket and re-parent the members under it, so the
        batch can be approved/implemented as one unit. Members keep their own
        status; the parent is born in backlog (Gate 1 stays human-only)."""
        members = await self.validate_group_members(repository_uid, member_ticket_uids)
        parent = await self.create(
            CreateTicketRequest(
                repository_uid=repository_uid,
                title=title,
                description=description,
                labels=list(labels or []),
                priority=priority if priority in TICKET_PRIORITIES else "medium",
                origin=origin,
            ),
            actor_uid=actor_uid,
        )
        now = datetime.now(UTC)
        for m in members:
            m.parent_ticket_uid = parent.uid
            m.updated_at = now
            await m.save()
        await write_audit(
            kind="ticket.grouped",
            subject_uid=parent.uid,
            subject_type="Ticket",
            actor_uid=actor_uid,
            payload={
                "repository_uid": repository_uid,
                "member_ticket_uids": [m.uid for m in members],
            },
        )
        return parent

    async def ungroup(self, parent_uid: str, *, actor_uid: str | None = None) -> int:
        """Dissolve a group: detach every child from the parent. The parent
        ticket itself is kept (delete it separately if unwanted)."""
        parent = await self.get_node(parent_uid)
        children = await Ticket.nodes.filter(parent_ticket_uid=parent_uid)
        if not children:
            raise HTTPException(status_code=409, detail="ticket has no subtickets to ungroup")
        now = datetime.now(UTC)
        for c in children:
            c.parent_ticket_uid = ""
            c.updated_at = now
            await c.save()
        await write_audit(
            kind="ticket.ungrouped",
            subject_uid=parent.uid,
            subject_type="Ticket",
            actor_uid=actor_uid,
            payload={"member_ticket_uids": [c.uid for c in children]},
        )
        return len(children)

    async def remove_from_group(self, uid: str, *, actor_uid: str | None = None) -> Ticket:
        """Detach a single ticket from its parent group."""
        t = await self.get_node(uid)
        if not (t.parent_ticket_uid or ""):
            raise HTTPException(status_code=409, detail="ticket is not part of a group")
        former_parent = t.parent_ticket_uid
        t.parent_ticket_uid = ""
        t.updated_at = datetime.now(UTC)
        await t.save()
        await write_audit(
            kind="ticket.left_group",
            subject_uid=t.uid,
            subject_type="Ticket",
            actor_uid=actor_uid,
            payload={"former_parent_ticket_uid": former_parent},
        )
        return t

    # ── Transitions ──────────────────────────────────────────────────────

    async def transition(
        self, uid: str, to_status: str, *, actor_uid: str, actor_role: str
    ) -> Ticket:
        """Human transition — matrix-checked; Gate-1 is role-gated."""
        t = await self.get_node(uid)
        frm = t.status or "backlog"
        if frm == to_status:
            raise HTTPException(status_code=409, detail=f"ticket is already {to_status}")
        if not is_legal_transition(frm, to_status):
            raise HTTPException(
                status_code=409, detail=f"illegal transition {frm} → {to_status}"
            )
        if (frm, to_status) == GATE_1:
            if not role_at_least(actor_role, "maintainer"):
                raise HTTPException(
                    status_code=403,
                    detail="Gate 1 (backlog → todo) requires role 'maintainer' or higher",
                )
            t.approved_by = actor_uid
            t.approved_at = datetime.now(UTC)
        await self._set_status(t, to_status)
        if (frm, to_status) == GATE_1:
            await write_audit(
                kind="ticket.approved",
                subject_uid=t.uid,
                subject_type="Ticket",
                actor_uid=actor_uid,
                payload={"approved_by": actor_uid},
            )
        await write_audit(
            kind="ticket.transitioned",
            subject_uid=t.uid,
            subject_type="Ticket",
            actor_uid=actor_uid,
            payload={"from": frm, "to": to_status},
        )
        return t

    async def _set_status(self, t: Ticket, to_status: str) -> None:
        t.status = to_status
        if to_status == "done":
            t.done_at = datetime.now(UTC)
        t.updated_at = datetime.now(UTC)
        await t.save()

    # ── Links ────────────────────────────────────────────────────────────

    async def link_finding(
        self, uid: str, finding_uid: str, *, actor_uid: str | None = None
    ) -> Ticket:
        """Idempotent append of a finding to the ticket."""
        t = await self.get_node(uid)
        linked = list(t.linked_finding_uids or [])
        if finding_uid not in linked:
            linked.append(finding_uid)
            t.linked_finding_uids = linked
            t.updated_at = datetime.now(UTC)
            await t.save()
            await write_audit(
                kind="ticket.finding_linked",
                subject_uid=t.uid,
                subject_type="Ticket",
                actor_uid=actor_uid,
                payload={"finding_uid": finding_uid},
            )
        return t

    async def link_pr(
        self,
        uid: str,
        pull_request_uid: str,
        *,
        actor_uid: str | None = None,
        auto_review: bool = True,
    ) -> Ticket:
        """Idempotent append of a PR; work under review auto-advances the ticket."""
        t = await self.get_node(uid)
        linked = list(t.linked_pr_uids or [])
        if pull_request_uid not in linked:
            linked.append(pull_request_uid)
            t.linked_pr_uids = linked
            t.updated_at = datetime.now(UTC)
            await t.save()
            await write_audit(
                kind="ticket.pr_linked",
                subject_uid=t.uid,
                subject_type="Ticket",
                actor_uid=actor_uid,
                payload={"pull_request_uid": pull_request_uid},
            )
        if auto_review and t.status in {"todo", "in-progress"}:
            frm = t.status
            await self._set_status(t, "in-review")
            await write_audit(
                kind="ticket.transitioned",
                subject_uid=t.uid,
                subject_type="Ticket",
                actor_uid="system",
                payload={"from": frm, "to": "in-review", "cause": "pr_linked"},
            )
        return t

    async def mark_done_via_merge(
        self, uid: str, *, pull_request_uid: str = ""
    ) -> Ticket:
        """Gate-2 follow-through: a merged linked PR completes the ticket."""
        t = await self.get_node(uid)
        if t.status == "done":
            return t
        frm = t.status
        await self._set_status(t, "done")
        await write_audit(
            kind="ticket.done_via_merge",
            subject_uid=t.uid,
            subject_type="Ticket",
            actor_uid="system",
            payload={"from": frm, "pull_request_uid": pull_request_uid},
        )
        return t

    # ── Delete ───────────────────────────────────────────────────────────

    async def delete(self, uid: str, *, actor_uid: str | None = None) -> None:
        t = await self.get_node(uid)
        if t.status != "backlog":
            raise HTTPException(
                status_code=409,
                detail=f"only backlog tickets are deletable (status={t.status})",
            )
        await t.delete()
        await write_audit(
            kind="ticket.deleted",
            subject_uid=uid,
            subject_type="Ticket",
            actor_uid=actor_uid,
            payload={},
        )
