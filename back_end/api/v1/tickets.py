"""Ticket routes — board CRUD, Gate-1 transition, finding/PR links (§15 Phase 2).

Gate 1 (backlog → todo) is the human approval gate: maintainer+ only, and it
records approved_by/approved_at. Status only ever moves through the transition
endpoint so every move is legality-checked and audited.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from api.dependencies import get_current_user, require_role
from domains.tenancy import org_repo_uids, require_repo_in_org
from domains.tickets.schemas import (
    CreateTicketRequest,
    GroupTicketsRequest,
    LinkFindingRequest,
    LinkPullRequestRequest,
    TicketDetailDTO,
    TicketDTO,
    TransitionTicketRequest,
    UpdateTicketRequest,
)
from domains.tickets.services.ticket_service import TicketService, ticket_to_dto
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])


@router.get("", response_model=list[TicketDTO], operation_id="opensweep_ticket_list")
async def list_tickets(
    repository_uid: str | None = Query(None),
    status: str | None = Query(None),
    origin: str | None = Query(None),
    parent_ticket_uid: str | None = Query(None),
    assignee_uid: str | None = Query(None),
    user: UserDTO = Depends(get_current_user),
):
    """Priority desc, then updated_at desc."""
    if repository_uid is not None:
        await require_repo_in_org(repository_uid, user.org_uid)
    tickets = await TicketService().list(
        repository_uid=repository_uid,
        status=status,
        origin=origin,
        parent_ticket_uid=parent_ticket_uid,
        assignee_uid=assignee_uid,
    )
    if repository_uid is None:
        allowed = await org_repo_uids(user.org_uid)
        tickets = [t for t in tickets if t.repository_uid in allowed]
    return tickets


# ── Grouping — batch related tickets under one parent ───────────────────────


@router.post("/group", response_model=TicketDTO, operation_id="opensweep_ticket_group")
async def group_tickets(
    req: GroupTicketsRequest, user: UserDTO = Depends(require_role("maintainer"))
):
    """Group ≥2 tickets under a new parent ticket so the batch can be
    approved and implemented as one unit. Members keep their own status; the
    parent is born in backlog (Gate 1 stays human-only)."""
    await require_repo_in_org(req.repository_uid, user.org_uid)
    parent = await TicketService().group_tickets(
        repository_uid=req.repository_uid,
        title=req.title,
        description=req.description,
        member_ticket_uids=req.member_ticket_uids,
        labels=req.labels,
        priority=req.priority,
        origin="human",
        actor_uid=user.uid,
    )
    return ticket_to_dto(parent)


class ProposeGroupsRequest(BaseModel):
    repository_uid: str = Field(min_length=1)


def _build_group_proposal_intent(tickets: list[TicketDTO], repository_uid: str) -> str:
    lines = []
    for t in tickets:
        desc = (t.description or "").strip().replace("\n", " ")
        if len(desc) > 200:
            desc = desc[:200] + "…"
        labels = ", ".join(t.labels or []) or "(none)"
        lines.append(
            f"- uid: {t.uid}\n"
            f"  title: {t.title}\n"
            f"  status: {t.status.value} · priority: {t.priority} · labels: {labels}\n"
            f"  description: {desc or '(none)'}"
        )
    listing = "\n".join(lines)
    return (
        "Analyze the open tickets below and propose which of them should be "
        "grouped into batches — sets of tickets that touch the same subsystem, "
        "the same files, or share one theme, so a single implement run (one PR "
        "or a small series) can pick up the whole batch instead of one PR per "
        "ticket. This is read-only against the repository — do not modify any "
        "code.\n"
        "\n"
        f"Repository uid: {repository_uid}\n"
        "\n"
        "Candidate tickets (ungrouped, backlog/todo):\n"
        f"{listing}\n"
        "\n"
        "Task:\n"
        "1. Read the code the tickets touch to judge which ones genuinely "
        "belong together. Overlapping files or one shared root cause are "
        "strong signals; a vague thematic echo is not.\n"
        "2. For each coherent batch of 2-6 tickets, call "
        "`opensweep_platform_propose_ticket_group` with the repository_uid, a "
        "short `title` for the batch, a `rationale` explaining why these "
        "tickets should ship together (cite the shared files/subsystem), the "
        "`member_ticket_uids`, and optionally `suggested_labels` and "
        "`suggested_priority`.\n"
        "3. Leave tickets that do not clearly belong to a batch ungrouped — "
        "a wrong grouping is worse than no grouping. Propose at most 4 "
        "groups, and never place one ticket in two groups.\n"
        "Do not create tickets, do not change ticket statuses, and do not "
        "file findings. A human reviews every proposal: approval creates the "
        "parent ticket with your members as subtickets; rejection discards it."
    )


@router.post("/propose-groups", operation_id="opensweep_ticket_propose_groups")
async def propose_ticket_groups(
    req: ProposeGroupsRequest, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    """Dispatch a read-only run that analyzes ungrouped backlog/todo tickets
    and proposes groupings via `opensweep_platform_propose_ticket_group`. Every
    proposal is human-approved before anything changes."""
    from domains.investigations.schemas import InvestigationEffort, RunTrigger
    from domains.investigations.services.lifecycle import LifecycleError, trigger_run
    from domains.run_policies.services.effort import ensure_policy_for_effort
    from infrastructure.audit import write_audit

    await require_repo_in_org(req.repository_uid, user.org_uid)
    tickets = await TicketService().list(repository_uid=req.repository_uid)
    candidates = [
        t
        for t in tickets
        if t.status.value in {"backlog", "todo"} and not t.parent_ticket_uid
    ]
    if len(candidates) < 2:
        raise HTTPException(
            status_code=409,
            detail="need at least 2 ungrouped backlog/todo tickets to propose groups",
        )

    # Specialized refine run: the grouping template IS the instructions
    # (custom_intent), so a replace overlay never displaces it; org append
    # guidance and the framing header/footer still stack.
    from domains.agent_overlays.services.composition import compose_playbook_intent

    composed = await compose_playbook_intent(
        repository_uid=req.repository_uid,
        playbook="refine",
        stage="refine",
        repo_guidance="",
        custom_intent=_build_group_proposal_intent(candidates, req.repository_uid),
        org_uid=user.org_uid,
    )
    intent = composed.text
    policy = await ensure_policy_for_effort(InvestigationEffort.NORMAL)
    await write_audit(
        kind="ticket_group.propose.requested",
        subject_uid=req.repository_uid,
        subject_type="Repository",
        actor_uid=user.uid,
        payload={"candidate_count": len(candidates)},
    )
    try:
        run = await trigger_run(
            repository_uid=req.repository_uid,
            intent=intent,
            playbook="refine",
            title="Propose ticket groups",
            target={"kind": "ticket-grouping"},
            run_policy_uid=policy.uid,
            trigger=RunTrigger.MANUAL,
            triggered_by=user.uid,
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "run_uid": run.uid,
        "investigation_uid": run.investigation_uid,
        "candidate_count": len(candidates),
    }


@router.post("/{uid}/ungroup", operation_id="opensweep_ticket_ungroup")
async def ungroup_ticket(
    uid: str, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    """Dissolve a group: detach every subticket from this parent. The parent
    ticket itself is kept."""
    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    detached = await service.ungroup(uid, actor_uid=user.uid)
    return {"ticket_uid": uid, "detached": detached}


@router.post(
    "/{uid}/remove-from-group",
    response_model=TicketDTO,
    operation_id="opensweep_ticket_remove_from_group",
)
async def remove_ticket_from_group(
    uid: str, user: UserDTO = Depends(require_role("maintainer"))
):
    """Detach this ticket from its parent group."""
    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    t = await service.remove_from_group(uid, actor_uid=user.uid)
    return ticket_to_dto(t)


@router.get("/{uid}", response_model=TicketDetailDTO, operation_id="opensweep_ticket_get")
async def get_ticket(uid: str, user: UserDTO = Depends(get_current_user)):
    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    return await service.get_detail(uid)


@router.post("", response_model=TicketDTO, operation_id="opensweep_ticket_create")
async def create_ticket(req: CreateTicketRequest, user: UserDTO = Depends(require_role("maintainer"))):
    await require_repo_in_org(req.repository_uid, user.org_uid)
    t = await TicketService().create(req, actor_uid=user.uid)
    return ticket_to_dto(t)


@router.patch("/{uid}", response_model=TicketDTO, operation_id="opensweep_ticket_update")
async def update_ticket(
    uid: str, req: UpdateTicketRequest, user: UserDTO = Depends(require_role("maintainer"))
):
    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    t = await service.update(uid, req, actor_uid=user.uid)
    return ticket_to_dto(t)


@router.post("/{uid}/status", response_model=TicketDTO, operation_id="opensweep_ticket_transition")
async def transition_ticket(
    uid: str, req: TransitionTicketRequest, user: UserDTO = Depends(get_current_user)
):
    """Legality-checked move; backlog → todo is Gate 1 (maintainer+, audited
    as ticket.approved). Illegal transitions → 409."""
    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    t = await service.transition(
        uid, req.status.value, actor_uid=user.uid, actor_role=user.role
    )
    return ticket_to_dto(t)


@router.post(
    "/{uid}/link-finding", response_model=TicketDTO, operation_id="opensweep_ticket_link_finding"
)
async def link_finding(
    uid: str, req: LinkFindingRequest, user: UserDTO = Depends(require_role("maintainer"))
):
    from domains.findings.models import Finding

    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    finding = await Finding.nodes.get_or_none(uid=req.finding_uid)
    if finding is None:
        raise HTTPException(status_code=404, detail="not found")
    await require_repo_in_org(finding.repository_uid, user.org_uid)
    if finding.repository_uid != ticket.repository_uid:
        raise HTTPException(status_code=409, detail="cross-repository link not allowed")
    t = await service.link_finding(uid, req.finding_uid, actor_uid=user.uid)
    return ticket_to_dto(t)


@router.post("/{uid}/link-pr", response_model=TicketDTO, operation_id="opensweep_ticket_link_pr")
async def link_pr(
    uid: str, req: LinkPullRequestRequest, user: UserDTO = Depends(require_role("maintainer"))
):
    from domains.delivery.models import PullRequest

    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    pr = await PullRequest.nodes.get_or_none(uid=req.pull_request_uid)
    if pr is None:
        raise HTTPException(status_code=404, detail="not found")
    await require_repo_in_org(pr.repository_uid, user.org_uid)
    if pr.repository_uid != ticket.repository_uid:
        raise HTTPException(status_code=409, detail="cross-repository link not allowed")
    t = await service.link_pr(uid, req.pull_request_uid, actor_uid=user.uid)
    return ticket_to_dto(t)


@router.post("/{uid}/implement", operation_id="opensweep_ticket_implement")
async def implement_ticket(uid: str, user: UserDTO = Depends(require_role("maintainer"))) -> dict:
    """Dispatch a write-path implement run for a Gate-1-approved ticket (§6).

    409 when the ticket hasn't passed Gate 1 or an open PR already implements
    it; an existing remote branch is adopted, not duplicated. The agent
    commits in a write sandbox; the platform validates, pushes, and opens a
    draft PR."""
    from domains.delivery.services.implement_run_service import trigger_implement_run
    from domains.investigations.services.lifecycle import LifecycleError

    ticket = await TicketService().get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    # A live thread owns this ticket's work branch — a parallel one-shot
    # implement run would race it on the branch and the fix-round ledger.
    from domains.threads.services.thread_service import ThreadService, has_active_thread

    if has_active_thread(await ThreadService().list(subject_ticket_uid=uid)):
        raise HTTPException(
            status_code=409,
            detail="this ticket has an active thread — approve implementation from the thread instead",
        )
    try:
        run = await trigger_implement_run(ticket, triggered_by=user.uid)
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "run_uid": run.uid,
        "investigation_uid": run.investigation_uid,
        "ticket_uid": uid,
    }


@router.post("/{uid}/refine", operation_id="opensweep_ticket_refine")
async def refine_ticket(uid: str, user: UserDTO = Depends(require_role("maintainer"))) -> dict:
    """Dispatch a read-only refine run that enriches the ticket in place —
    sharpening its title, description and acceptance criteria and attaching an
    implementation plan + relevant files via the platform tools."""
    from domains.tickets.services.refine_dispatch import dispatch_refine_run

    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    run = await dispatch_refine_run(ticket, actor_uid=user.uid, org_uid=user.org_uid)
    return {"run_uid": run.uid, "investigation_uid": run.investigation_uid, "ticket_uid": uid}


@router.delete("/{uid}", status_code=204, operation_id="opensweep_ticket_delete")
async def delete_ticket(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    """Backlog-only deletable — anything approved keeps its history (409 otherwise)."""
    service = TicketService()
    ticket = await service.get_node(uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    await service.delete(uid, actor_uid=user.uid)
    return Response(status_code=204)
