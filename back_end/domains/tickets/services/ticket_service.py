"""Ticket lifecycle — CRUD + Gate-1 state machine (PLATFORM_V2_DESIGN.md §2, §15 Phase 2).

Transition matrix (human API; every move audited):

    backlog ──► todo            GATE 1 — maintainer+ only; records approved_by/at
                                refused on an epic member: the parent is the
                                only thing approved, and it ships them all
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

# Imported, not redefined: this ladder had three divergent copies. Re-exported
# because callers and tests already import `priority_rank` from this module.
from infrastructure.ranking import priority_rank  # noqa: F401

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

def is_legal_transition(from_status: str, to_status: str) -> bool:
    return to_status in LEGAL_TRANSITIONS.get(from_status, frozenset())


def ensure_archivable(t, threads: list, children: list) -> None:
    """Archive guards, pure so the matrix is unit-testable without Neo4j.

    Any status may archive — archive is the reversible "leave the board",
    not a lifecycle move. Three refusals:
    - an active thread owns the ticket's work (name it: Abandon is the fix),
    - an epic member archives through its parent, never alone,
    - an epic parent with live members would strand them invisible.
    """
    active = [th for th in threads if getattr(th, "phase", "") not in ("done", "abandoned")]
    if active:
        raise HTTPException(
            status_code=409,
            detail=(
                f"this ticket has an active thread ({active[0].uid}) — "
                "abandon or finish it first"
            ),
        )
    if getattr(t, "parent_ticket_uid", "") or "":
        raise HTTPException(
            status_code=409,
            detail=(
                "this ticket belongs to an epic — archive the epic parent, "
                "or remove it from the epic first"
            ),
        )
    live = [
        c
        for c in children
        if (getattr(c, "status", "") or "") != "done" and not getattr(c, "archived", False)
    ]
    if live:
        raise HTTPException(
            status_code=409,
            detail=(
                f"this epic still has {len(live)} unfinished subticket"
                f"{'' if len(live) == 1 else 's'} — dissolve the epic or wait "
                "for them to finish"
            ),
        )


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
        severity=t.severity or "",
        kind=t.kind or "",
        tags=list(t.tags or []),
        subtype=t.subtype or "",
        origin=t.origin or "human",
        origin_finding_uid=t.origin_finding_uid or "",
        parent_ticket_uid=t.parent_ticket_uid or "",
        linked_finding_uids=list(t.linked_finding_uids or []),
        linked_pr_uids=list(t.linked_pr_uids or []),
        assignee_uid=t.assignee_uid or "",
        plan=dict(t.plan or {}),
        approved_by=t.approved_by or "",
        approved_at=t.approved_at,
        # getattr: nodes written before m0021 (and test doubles) may lack it.
        archived=bool(getattr(t, "archived", False)),
        archived_at=getattr(t, "archived_at", None),
        archived_by=getattr(t, "archived_by", "") or "",
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

    async def _require_finding_in_repo(self, finding_uid: str, repository_uid: str) -> Finding:
        """404 unless `finding_uid` exists AND lives in `repository_uid` (F4).

        Returns the finding so promotion can copy its epic facets without
        a second read.
        """
        finding = await Finding.nodes.get_or_none(uid=finding_uid)
        if finding is None or finding.repository_uid != repository_uid:
            raise HTTPException(status_code=404, detail=f"Finding {finding_uid} not found")
        return finding

    async def list(
        self,
        *,
        repository_uids: list[str],
        status: str | None = None,
        origin: str | None = None,
        parent_ticket_uid: str | None = None,
        assignee_uid: str | None = None,
        archived: bool = False,
    ) -> list[TicketDTO]:
        """Tickets in ``repository_uids`` — the caller's tenancy scope, so an
        empty list means an empty result, never "every ticket"."""
        if not repository_uids:
            return []
        filters: dict = {"repository_uid__in": repository_uids}
        if status:
            filters["status"] = status
        if origin:
            filters["origin"] = origin
        if parent_ticket_uid:
            filters["parent_ticket_uid"] = parent_ticket_uid
        if assignee_uid:
            filters["assignee_uid"] = assignee_uid
        nodes = await Ticket.nodes.filter(**filters)
        # Python-side, not Cypher: pre-m0021 nodes read None for `archived`,
        # which must count as not-archived. Default False silently fixes every
        # list caller (board, suggest-epics, MCP) to active-only; True is the
        # archived view — there is deliberately no "both" mode.
        nodes = [t for t in nodes if bool(getattr(t, "archived", False)) == archived]
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
        # Epic planning facets are derived HERE rather than at each promotion site
        # (resolution_service, the platform tool, bulk promote from the
        # findings board) so no caller can create a finding-backed ticket that
        # is invisible to severity/lens/class selection. An explicit value on
        # the request wins — callers that already hold the finding may pass it.
        finding = None
        if req.origin_finding_uid:
            finding = await self._require_finding_in_repo(
                req.origin_finding_uid, req.repository_uid
            )
        # Lexical near-duplicate check — the minimal precursor to the semantic
        # dedupe design in ticket 5570ff15. Skipped when:
        #   - the caller explicitly asserts distinctness (`allow_duplicate`),
        #   - the ticket is being promoted from a finding (the finding is what
        #     was dedup'd; promotion is a specific user-approved action),
        #   - the ticket is being created as a subticket (the parent already
        #     collects the work; a subticket sharing a title is a legitimate
        #     grouping choice, not a duplicate).
        if (
            not req.allow_duplicate
            and not req.origin_finding_uid
            and not req.parent_ticket_uid
        ):
            from domains.tickets.services.dedupe import find_open_ticket_duplicate

            existing = await find_open_ticket_duplicate(
                repository_uid=req.repository_uid, title=req.title
            )
            if existing is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "duplicate_ticket",
                        "message": (
                            f"an open ticket with a near-identical title already "
                            f"exists ({existing.uid}). Update or comment on it, "
                            "or re-submit with allow_duplicate=true if this is "
                            "genuinely distinct."
                        ),
                        "existing_ticket_uid": existing.uid,
                        "existing_title": existing.title or "",
                        "existing_status": existing.status or "backlog",
                    },
                )
        t = Ticket(
            uid=uuid4().hex,
            repository_uid=req.repository_uid,
            title=req.title,
            description=req.description,
            acceptance_criteria=req.acceptance_criteria,
            labels=req.labels,
            priority=req.priority or "medium",
            size=req.size,
            severity=req.severity or (finding.severity or "" if finding else ""),
            kind=req.kind or (finding.kind or "" if finding else ""),
            tags=req.tags or (list(finding.tags or []) if finding else []),
            subtype=req.subtype or (finding.subtype or "" if finding else ""),
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
        # A promoted finding should not keep sitting on the board as untouched
        # work — mark_ticketed moves it to "ticketed" and links it forward.
        # Deliberately NOT wrapped in try/except: the finding was already
        # existence-checked by _require_finding_in_repo above, so a failure
        # here is a real fault and should surface as a 500 rather than a
        # success that quietly did half the job. (Either way the ticket is
        # already committed — this buys a loud signal, not atomicity.)
        if finding is not None:
            from domains.findings.services.finding_service import FindingService

            await FindingService().mark_ticketed(
                finding.uid, ticket_uid=t.uid, actor_uid=actor_uid
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
            # State-machine correctness: `validate_group_members` refuses to
            # group a ticket that is past its own Gate 1, because none of its
            # own transitions re-check GATE_1 once it's past backlog. Without
            # the same guard here, `PATCH /tickets/{uid}` would let a
            # todo/in-progress ticket be re-parented into an epic and keep
            # moving on its own branch while the epic's single PR is also
            # targeting its files — silently breaking one-epic-one-PR.
            if (t.status or "backlog") != "backlog":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"ticket {uid} is {t.status or 'backlog'} — only "
                        "backlog tickets can be re-parented into an epic"
                    ),
                )
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

    # ── Grouping (epic related tickets under one parent) ────────────────

    async def validate_group_members(
        self, repository_uid: str, member_ticket_uids: list[str]
    ) -> list[Ticket]:
        """Resolve + validate a grouping's member set: ≥2 unique tickets,
        all in `repository_uid`, ALL in `backlog`. Order-preserving.

        Backlog-only, not merely "not done": a ticket past its own Gate 1
        (todo/in-progress/in-review) is already an implementable unit of
        work, and none of its own transitions re-check GATE_1 once it's
        past backlog. Grouping it into an epic would let it keep moving on
        its own branch while the epic's single PR is also targeting its
        files — silently breaking the "one epic, one PR" invariant.
        """
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
            if bool(getattr(t, "archived", False)):
                raise HTTPException(
                    status_code=409, detail=f"Ticket {uid} is archived — unarchive it first"
                )
            if (t.status or "backlog") != "backlog":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Ticket {uid} is {t.status or 'backlog'} — only "
                        "backlog tickets can be grouped into an epic (the "
                        "one-epic-one-PR invariant)"
                    ),
                )
            members.append(t)
        return members

    async def create_epic(
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
        epic can be approved/implemented as one unit. Members keep their own
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
                # Epic parent is a wrapper, not a work item: its title often
                # echoes its members' theme (e.g. "backend/api · 3 tickets"),
                # so a strict dedupe check would 409 the whole approval on
                # obvious title overlap. The member set is the identity of an
                # epic; leave title-level dedupe to the members themselves.
                allow_duplicate=True,
            ),
            actor_uid=actor_uid,
        )
        now = datetime.now(UTC)
        for m in members:
            m.parent_ticket_uid = parent.uid
            m.updated_at = now
            await m.save()
        await write_audit(
            kind="epic.created",
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
        ticket itself is kept (delete it separately if unwanted).

        Refused once the epic has left `backlog`: `mark_done_via_merge`
        iterates `parent_ticket_uid=uid` at merge time, so a member cleared
        from that link mid-flight never advances to `done` when the epic's
        one PR lands — it gets stranded in `todo`/`in-progress` with no
        audit trail tying it back to the run that in fact delivered it.
        """
        parent = await self.get_node(parent_uid)
        if (parent.status or "backlog") != "backlog":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"epic parent {parent.uid} is past backlog (status="
                    f"{parent.status or 'backlog'}) — a run is already targeting "
                    "its members. Wait for the PR to merge (or send the parent "
                    "back to backlog) before dissolving the group."
                ),
            )
        children = await Ticket.nodes.filter(parent_ticket_uid=parent_uid)
        if not children:
            raise HTTPException(status_code=409, detail="ticket has no subtickets to ungroup")
        now = datetime.now(UTC)
        for c in children:
            c.parent_ticket_uid = ""
            c.updated_at = now
            await c.save()
        await write_audit(
            kind="epic.dissolved",
            subject_uid=parent.uid,
            subject_type="Ticket",
            actor_uid=actor_uid,
            payload={"member_ticket_uids": [c.uid for c in children]},
        )
        return len(children)

    async def remove_from_group(self, uid: str, *, actor_uid: str | None = None) -> Ticket:
        """Detach a single ticket from its parent group.

        Refused when the parent epic has left `backlog` — see `ungroup` for
        why: `mark_done_via_merge` only closes members whose
        `parent_ticket_uid` still points at the merging epic, so a detached
        member is stranded outside `done` when the epic's PR lands.
        """
        t = await self.get_node(uid)
        if not (t.parent_ticket_uid or ""):
            raise HTTPException(status_code=409, detail="ticket is not part of a group")
        parent = await Ticket.nodes.get_or_none(uid=t.parent_ticket_uid)
        if parent is not None and (parent.status or "backlog") != "backlog":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"epic parent {parent.uid} is past backlog (status="
                    f"{parent.status or 'backlog'}) — the run targeting this "
                    "member is in flight. Wait for the PR to merge (or send "
                    "the parent back to backlog) before removing this member."
                ),
            )
        former_parent = t.parent_ticket_uid
        t.parent_ticket_uid = ""
        t.updated_at = datetime.now(UTC)
        await t.save()
        await write_audit(
            kind="epic.member_removed",
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
        if bool(getattr(t, "archived", False)):
            raise HTTPException(
                status_code=409, detail="ticket is archived — unarchive it first"
            )
        frm = t.status or "backlog"
        if frm == to_status:
            raise HTTPException(status_code=409, detail=f"ticket is already {to_status}")
        if not is_legal_transition(frm, to_status):
            raise HTTPException(
                status_code=409, detail=f"illegal transition {frm} → {to_status}"
            )
        if (frm, to_status) == GATE_1:
            # An epic ships as ONE run and ONE PR against the parent, so the
            # parent is the only thing that passes Gate 1. Approving a member
            # would make it individually implementable — a second branch racing
            # the epic's PR over the same files. Ungroup it first if it really
            # is separate work.
            if t.parent_ticket_uid:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "this ticket belongs to an epic — approve the epic parent "
                        f"({t.parent_ticket_uid}), which ships every subticket in one "
                        "run. Remove it from the epic to work it on its own."
                    ),
                )
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
        """Gate-2 follow-through: a merged linked PR completes the ticket.

        Group flow (unified dev flow): a group parent is implemented as ONE
        unit — merging its PR completes every subticket too
        ("ticket.done_via_epic_merge").
        """
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
        for child in await Ticket.nodes.filter(parent_ticket_uid=uid):
            if child.status == "done":
                continue
            child_frm = child.status
            await self._set_status(child, "done")
            await write_audit(
                kind="ticket.done_via_epic_merge",
                subject_uid=child.uid,
                subject_type="Ticket",
                actor_uid="system",
                payload={
                    "from": child_frm,
                    "parent_ticket_uid": uid,
                    "pull_request_uid": pull_request_uid,
                },
            )
        await self._complete_parent_if_all_children_done(t)
        return t

    async def _complete_parent_if_all_children_done(self, child: Ticket) -> None:
        """Close a group parent once every member is done.

        The normal path is top-down: the epic's single PR merges and
        `mark_done_via_merge` closes every member. This is the bottom-up
        complement, for epics finished any other way — a member closed by its
        own PR, by a manual transition, or an epic dissolved by hand. Without
        it such an epic sits in `todo` forever, making fully-delivered work
        look permanently in flight on the board.

        Event-driven rather than swept, so it holds however a member got
        closed. (m0019 removed the epic-dispatch tick that once fanned an epic
        into one run per member; nothing polls epics any more.)

        Recursion-safe: closing the parent re-enters `mark_done_via_merge`,
        whose `status == "done"` guard short-circuits on the second pass.
        """
        parent_uid = child.parent_ticket_uid or ""
        if not parent_uid:
            return
        parent = await Ticket.nodes.get_or_none(uid=parent_uid)
        if parent is None or parent.status == "done":
            return
        siblings = await Ticket.nodes.filter(parent_ticket_uid=parent_uid)
        if not all((s.status or "") == "done" for s in siblings):
            return
        frm = parent.status
        await self._set_status(parent, "done")
        await write_audit(
            kind="ticket.done_via_epic_rollup",
            subject_uid=parent.uid,
            subject_type="Ticket",
            actor_uid="system",
            payload={"from": frm, "member_count": len(siblings)},
        )

    # ── Archive ──────────────────────────────────────────────────────────

    async def archive(self, uid: str, *, actor_uid: str) -> Ticket:
        """Reversible "leave the board" from any status — guards in
        `ensure_archivable`. Idempotent."""
        t = await self.get_node(uid)
        if bool(getattr(t, "archived", False)):
            return t
        from domains.threads.models import Thread

        threads = await Thread.nodes.filter(subject_ticket_uid=uid)
        children = await Ticket.nodes.filter(parent_ticket_uid=uid)
        ensure_archivable(t, list(threads), list(children))
        now = datetime.now(UTC)
        t.archived = True
        t.archived_at = now
        t.archived_by = actor_uid
        t.updated_at = now
        await t.save()
        await write_audit(
            kind="ticket.archived",
            subject_uid=t.uid,
            subject_type="Ticket",
            actor_uid=actor_uid,
            payload={"status": t.status or ""},
        )
        return t

    async def unarchive(self, uid: str, *, actor_uid: str) -> Ticket:
        """Restore an archived ticket to its lane (status was never touched)."""
        t = await self.get_node(uid)
        if not bool(getattr(t, "archived", False)):
            return t
        t.archived = False
        t.archived_at = None
        t.archived_by = ""
        t.updated_at = datetime.now(UTC)
        await t.save()
        await write_audit(
            kind="ticket.unarchived",
            subject_uid=t.uid,
            subject_type="Ticket",
            actor_uid=actor_uid,
            payload={"status": t.status or ""},
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
        # Read the origin off the node while it still exists.
        origin_finding_uid = t.origin_finding_uid or ""
        await t.delete()
        # Audit BEFORE the release: the release is two more round-trips, and if
        # either raises, the ticket is already gone — writing the record last
        # would make a completed deletion invisible to the audit trail.
        await write_audit(
            kind="ticket.deleted",
            subject_uid=uid,
            subject_type="Ticket",
            actor_uid=actor_uid,
            payload={},
        )
        # This ticket may be the only reason a finding left the board, so
        # deleting it must put that finding back rather than stranding it as
        # "ticketed" pointing at nothing. No-ops unless the finding still
        # points at this ticket.
        if origin_finding_uid:
            from domains.findings.services.finding_service import FindingService

            await FindingService().clear_ticket(
                origin_finding_uid, ticket_uid=uid, actor_uid=actor_uid
            )
