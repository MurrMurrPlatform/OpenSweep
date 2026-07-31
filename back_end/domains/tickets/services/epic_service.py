"""Ticket group proposals — agent-suggested epics, human-approved.

Mirrors the Gate-1 contract: agents may only PROPOSE a grouping (via the
platform tool); a maintainer approves or rejects it. Approval materializes a
parent Ticket (origin agent-proposal, born in backlog) and re-parents the
member tickets under it; the members' own statuses are never touched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from domains.tickets.models import TICKET_PRIORITIES, EpicProposal, Ticket
from domains.tickets.schemas import EpicProposalDTO, EpicProposalStatus
from domains.tickets.services.epics.schemas import (
    AGENT_AXES,
    MAX_RATIONALE_CHARS,
    EpicAxis,
)
from domains.tickets.services.ticket_service import TicketService
from infrastructure.audit import write_audit

EPIC_AXES = {a.value for a in EpicAxis}

#: The axes an agent-authored proposal may claim. `EpicAxis` members are
#: compared by value everywhere here — a StrEnum hashes by NAME, so a set of
#: members would never contain the raw string an agent sends.
AGENT_AXIS_VALUES = {a.value for a in AGENT_AXES}


def proposal_to_dto(
    p: EpicProposal, *, member_titles: list[str] | None = None
) -> EpicProposalDTO:
    return EpicProposalDTO(
        uid=p.uid,
        repository_uid=p.repository_uid,
        title=p.title or "",
        rationale=p.rationale or "",
        member_ticket_uids=list(p.member_ticket_uids or []),
        member_titles=list(member_titles or []),
        suggested_labels=list(p.suggested_labels or []),
        suggested_priority=p.suggested_priority or "medium",
        axis=p.axis or "root-cause",
        evidence=dict(p.evidence or {}),
        plan_uid=p.plan_uid or "",
        origin=p.origin or "agent",
        status=EpicProposalStatus(p.status or "proposed"),
        source_run_uid=p.source_run_uid or "",
        created_ticket_uid=p.created_ticket_uid or "",
        reviewed_by=p.reviewed_by or "",
        reviewed_at=p.reviewed_at,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


class EpicService:
    async def get_node(self, uid: str) -> EpicProposal:
        p = await EpicProposal.nodes.get_or_none(uid=uid)
        if p is None:
            raise HTTPException(status_code=404, detail=f"EpicProposal {uid} not found")
        return p

    async def list(
        self, *, repository_uid: str | None = None, status: str | None = None
    ) -> list[EpicProposalDTO]:
        filters: dict = {}
        if repository_uid:
            filters["repository_uid"] = repository_uid
        if status:
            filters["status"] = status
        nodes = await (
            EpicProposal.nodes.filter(**filters)
            if filters
            else EpicProposal.nodes.all()
        )
        nodes = list(nodes)

        # Resolve member titles here rather than letting the client match uids
        # against whatever tickets its board happened to have loaded — that
        # rendered filtered-out members as raw uid fragments. One read per
        # repository, not one per member.
        titles: dict[str, str] = {}
        repo_uids = {p.repository_uid for p in nodes if p.repository_uid}
        for repo_uid in repo_uids:
            for t in await Ticket.nodes.filter(repository_uid=repo_uid):
                titles[t.uid] = t.title or ""

        out = [
            proposal_to_dto(
                p,
                member_titles=[
                    titles.get(uid, "") for uid in (p.member_ticket_uids or [])
                ],
            )
            for p in nodes
        ]
        floor = datetime.min.replace(tzinfo=UTC)
        out.sort(key=lambda d: d.created_at or floor, reverse=True)
        return out

    async def propose(
        self,
        *,
        repository_uid: str,
        title: str,
        rationale: str = "",
        member_ticket_uids: list[str],
        suggested_labels: list[str] | None = None,
        suggested_priority: str = "medium",
        source_run_uid: str = "",
        actor_uid: str | None = None,
        axis: str = "root-cause",
        evidence: dict | None = None,
        plan_uid: str = "",
        origin: str = "agent",
    ) -> tuple[EpicProposal, bool]:
        """Record a grouping proposal. Idempotent on the member set: an open
        proposal for the same repository with the same members is returned
        instead of duplicated. Returns (proposal, deduplicated)."""
        members = await TicketService().validate_group_members(
            repository_uid, member_ticket_uids
        )
        member_uids = [m.uid for m in members]

        existing = await EpicProposal.nodes.filter(
            repository_uid=repository_uid, status="proposed"
        )
        for p in existing:
            if set(p.member_ticket_uids or []) == set(member_uids):
                return p, True

        # An agent may only claim an axis a model is actually needed for. One
        # stamping `axis='area'` would render as a computed grouping on the
        # review card — and a reviewer's whole reason for trusting those is
        # that no model was involved in making them.
        allowed = AGENT_AXIS_VALUES if origin == "agent" else EPIC_AXES
        axis = axis if axis in allowed else EpicAxis.ROOT_CAUSE.value
        p = EpicProposal(
            uid=uuid4().hex,
            repository_uid=repository_uid,
            title=title,
            # Agents wrote unbounded prose here and the review card rendered
            # all of it as markdown — the verbosity starts at the write, so
            # the clamp does too.
            rationale=(rationale or "")[:MAX_RATIONALE_CHARS],
            member_ticket_uids=member_uids,
            suggested_labels=list(suggested_labels or []),
            # Agents send free text — clamp to the priority vocabulary.
            suggested_priority=(
                suggested_priority if suggested_priority in TICKET_PRIORITIES else "medium"
            ),
            axis=axis,
            evidence=dict(evidence or {}),
            plan_uid=plan_uid,
            origin=origin,
            status="proposed",
            source_run_uid=source_run_uid,
        )
        await p.save()
        await write_audit(
            kind="epic.proposed",
            subject_uid=p.uid,
            subject_type="EpicProposal",
            actor_uid=actor_uid,
            payload={
                "repository_uid": repository_uid,
                "title": title,
                "member_ticket_uids": member_uids,
                "source_run_uid": source_run_uid,
            },
        )
        return p, False

    async def approve(self, uid: str, *, actor_uid: str) -> EpicProposalDTO:
        """Human approval: materialize the parent ticket and re-parent the
        members. Members that disappeared, finished, or were meanwhile claimed
        by another epic are dropped; if fewer than 2 remain the proposal is
        stale (409)."""
        p = await self.get_node(uid)
        if p.status != "proposed":
            raise HTTPException(status_code=409, detail=f"proposal is already {p.status}")

        alive: list[str] = []
        claimed: list[str] = []
        for member_uid in list(p.member_ticket_uids or []):
            t = await Ticket.nodes.get_or_none(uid=member_uid)
            if t is None or t.status == "done" or t.repository_uid != p.repository_uid:
                continue
            # Already in another epic. `partition` keeps a ticket out of two
            # epics within ONE plan, but nothing stops two live proposals
            # from different producers (a rule plan and an agent plan) sharing
            # a member — and `create_epic` would happily overwrite
            # parent_ticket_uid, silently moving the ticket out of the epic
            # that was approved first. It would vanish from that parent's
            # children with no error and no audit trail. Drop it instead: the
            # first approval wins, which is the only outcome a reviewer can
            # actually predict.
            if t.parent_ticket_uid:
                claimed.append(member_uid)
                continue
            alive.append(member_uid)
        if len(alive) < 2:
            detail = "proposal is stale — fewer than 2 member tickets are still open"
            if claimed:
                detail = (
                    "proposal is stale — "
                    f"{len(claimed)} member ticket(s) were already in another epic, "
                    "leaving fewer than 2"
                )
            raise HTTPException(status_code=409, detail=detail)

        parent = await TicketService().create_epic(
            repository_uid=p.repository_uid,
            title=p.title,
            description=p.rationale or "",
            member_ticket_uids=alive,
            labels=list(p.suggested_labels or []),
            priority=p.suggested_priority or "medium",
            origin="agent-proposal",
            actor_uid=actor_uid,
        )

        # Approving the epic IS the Gate-1 approval for the parent it
        # creates. Previously the parent was born in `backlog`, so a reviewer
        # who had just said "yes, ship these together" had to find the new
        # parent and approve it a second time. Collapsing the two clicks does
        # not weaken the gate: a maintainer still made one explicit decision,
        # and it is recorded on the parent exactly as a manual Gate-1 move is.
        now = datetime.now(UTC)
        parent.status = "todo"
        parent.approved_by = actor_uid
        parent.approved_at = now
        parent.updated_at = now
        await parent.save()
        await write_audit(
            kind="ticket.transitioned",
            subject_uid=parent.uid,
            subject_type="Ticket",
            actor_uid=actor_uid,
            payload={"from": "backlog", "to": "todo", "cause": "epic_approval"},
        )

        # Member statuses are deliberately left alone. An epic ships as ONE
        # run on the parent — `trigger_implement_run` injects the members as
        # its work list and the merged PR closes every one of them — so a
        # member is never dispatched against directly and never needs to pass
        # Gate 1 on its own.
        p.status = "approved"
        p.created_ticket_uid = parent.uid
        p.reviewed_by = actor_uid
        p.reviewed_at = now
        p.updated_at = now
        await p.save()
        await write_audit(
            kind="epic.approved",
            subject_uid=p.uid,
            subject_type="EpicProposal",
            actor_uid=actor_uid,
            payload={
                "created_ticket_uid": parent.uid,
                "member_ticket_uids": alive,
                "axis": p.axis or "",
                # Members silently absent from the epic a reviewer thought
                # they were approving — recorded so the drop is inspectable.
                "dropped_already_grouped": claimed,
            },
        )
        return proposal_to_dto(p)

    async def bulk_approve(
        self, uids: list[str], *, actor_uid: str
    ) -> dict[str, list]:
        """Approve several epics as one review action.

        Partial success is reported, not raised: a plan of four epics where
        one went stale should still land the other three, and the caller needs
        to know which failed and why. Raising on the first failure would make
        the outcome depend on dict ordering.
        """
        approved: list[EpicProposalDTO] = []
        errors: list[dict[str, str]] = []
        for uid in uids:
            try:
                approved.append(await self.approve(uid, actor_uid=actor_uid))
            except HTTPException as exc:
                errors.append({"uid": uid, "detail": str(exc.detail)})
        return {"approved": approved, "errors": errors}

    async def reject(self, uid: str, *, actor_uid: str) -> EpicProposalDTO:
        p = await self.get_node(uid)
        if p.status != "proposed":
            raise HTTPException(status_code=409, detail=f"proposal is already {p.status}")
        p.status = "rejected"
        p.reviewed_by = actor_uid
        p.reviewed_at = datetime.now(UTC)
        p.updated_at = p.reviewed_at
        await p.save()
        await write_audit(
            kind="epic.rejected",
            subject_uid=p.uid,
            subject_type="EpicProposal",
            actor_uid=actor_uid,
            payload={},
        )
        return proposal_to_dto(p)
