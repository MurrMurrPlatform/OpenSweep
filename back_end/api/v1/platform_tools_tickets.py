"""HTTP transport for the ticket platform tools (PLATFORM_V2_DESIGN.md §11).

Executor-facing surface: agents may PROPOSE work (create a backlog ticket with
origin agent-proposal) and read tickets. There is deliberately NO transition
tool here — Gate 1 (backlog → todo) is human-only, so agents can never promote
their own proposals into implementable work.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.dependencies import get_current_user
from api.platform_scope import require_tool_repo_access
from domains.tickets.models import Ticket
from domains.tickets.schemas import CreateTicketRequest, TicketDTO, UpdateTicketRequest
from domains.tickets.services.ticket_service import TicketService, ticket_to_dto
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1/platform-tools/tickets", tags=["platform_tools"])


class PlatformCreateTicketRequest(BaseModel):
    """Like CreateTicketRequest, minus origin/status — both are forced."""

    title: str = Field(min_length=1)
    repository_uid: str = Field(min_length=1)
    description: str = Field(
        "",
        description=(
            "What needs to happen and why, with enough context to implement "
            "without re-deriving the analysis. Rendered as markdown."
        ),
    )
    acceptance_criteria: list[str] = Field(
        default_factory=list,
        description=(
            "2-6 short, independently testable criteria (one clause each, plain "
            "text). Review runs verify every criterion against the PR — always "
            "provide them; a ticket without criteria cannot be meaningfully "
            "verified."
        ),
    )
    labels: list[str] = Field(default_factory=list)
    priority: str = "medium"
    size: str = ""
    origin_finding_uid: str = ""
    parent_ticket_uid: str = ""
    allow_duplicate: bool = Field(
        default=False,
        description=(
            "Set to true ONLY after inspecting the near-duplicate ticket the "
            "platform returned on a prior 409 (`existing_ticket_uid`) and "
            "confirming the new one is genuinely distinct. Default false so "
            "the tool refuses to file an obvious duplicate."
        ),
    )


@router.post(
    "/create",
    response_model=TicketDTO,
    operation_id="opensweep_platform_create_ticket",
)
async def platform_create_ticket(
    req: PlatformCreateTicketRequest, request: Request, user: UserDTO = Depends(get_current_user)
):
    """Agents propose work: origin is forced to agent-proposal and the ticket
    lands in backlog — never todo (Gate 1 is human-only).

    Refuses to file a near-duplicate of an existing open ticket in the same
    repo (409) — pass `allow_duplicate=true` after reviewing the returned
    `existing_ticket_uid` if the new work really is distinct.
    """
    await require_tool_repo_access(request, user, req.repository_uid)
    actor = request.headers.get("X-OpenSweep-Run-Uid") or user.uid
    t = await TicketService().create(
        CreateTicketRequest(
            title=req.title,
            repository_uid=req.repository_uid,
            description=req.description,
            acceptance_criteria=req.acceptance_criteria,
            labels=req.labels,
            priority=req.priority,
            size=req.size,
            origin="agent-proposal",
            origin_finding_uid=req.origin_finding_uid,
            parent_ticket_uid=req.parent_ticket_uid,
            allow_duplicate=req.allow_duplicate,
        ),
        actor_uid=actor,
    )
    return ticket_to_dto(t)


class PlatformUpdateTicketRequest(BaseModel):
    """Refine-run field edits. Status never moves here — it goes through the
    human transition endpoint. Only non-null fields are applied."""

    ticket_uid: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1)
    description: str | None = Field(
        default=None,
        description="Improved description — what/why, enough to implement. Markdown.",
    )
    acceptance_criteria: list[str] | None = Field(
        default=None,
        description="2-6 short, independently testable criteria (one clause each).",
    )
    labels: list[str] | None = None
    priority: str | None = None
    size: str | None = None


@router.post(
    "/update",
    response_model=TicketDTO,
    operation_id="opensweep_platform_update_ticket",
)
async def platform_update_ticket(
    req: PlatformUpdateTicketRequest, request: Request, user: UserDTO = Depends(get_current_user)
):
    """Refine runs sharpen a ticket in place: title, description, acceptance
    criteria, labels, priority, size. Status is untouched (Gate 1 stays
    human-only)."""
    t = await Ticket.nodes.get_or_none(uid=req.ticket_uid)
    if t is None:
        raise HTTPException(status_code=404, detail="not found")
    await require_tool_repo_access(request, user, t.repository_uid)
    actor = request.headers.get("X-OpenSweep-Run-Uid") or user.uid
    patch = req.model_dump(exclude={"ticket_uid"}, exclude_none=True)
    updated = await TicketService().update(
        req.ticket_uid, UpdateTicketRequest(**patch), actor_uid=actor
    )
    return ticket_to_dto(updated)


class PlatformTransitionTicketRequest(BaseModel):
    """Move a ticket through the status matrix. Only permitted when the repo
    has opted into agent autonomy (Repository.agent_autonomy)."""

    ticket_uid: str = Field(min_length=1)
    to_status: str = Field(
        min_length=1,
        description=(
            "Target status: backlog | todo | in-progress | in-review | done. "
            "Only legal matrix transitions are accepted (illegal → 409). "
            "backlog → todo is the Gate-1 approval."
        ),
    )


@router.post(
    "/transition",
    response_model=TicketDTO,
    operation_id="opensweep_platform_transition_ticket",
)
async def platform_transition_ticket(
    req: PlatformTransitionTicketRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    """Agent-driven ticket status transition — gated on repo agent autonomy.

    By default this is human-only (Gate 1): an agent must not promote its own
    proposed work. When the operator has enabled `agent_autonomy` for the repo,
    the agent transitions with maintainer authority, so the Gate-1 role check
    passes; the status matrix and epic-member rules still apply.
    """
    from domains.repositories.services.agent_capabilities import (
        agent_autonomy_enabled,
    )

    t = await Ticket.nodes.get_or_none(uid=req.ticket_uid)
    if t is None:
        raise HTTPException(status_code=404, detail="not found")
    await require_tool_repo_access(request, user, t.repository_uid)
    if not await agent_autonomy_enabled(t.repository_uid):
        raise HTTPException(
            status_code=403,
            detail=(
                "agent autonomy is disabled for this repository — ticket status "
                "moves through a human (Gate 1). Enable it in repository settings "
                "to let agents transition tickets."
            ),
        )
    actor = request.headers.get("X-OpenSweep-Run-Uid") or user.uid
    updated = await TicketService().transition(
        req.ticket_uid, req.to_status, actor_uid=actor, actor_role="maintainer"
    )
    return ticket_to_dto(updated)


class PlatformProposeEpicRequest(BaseModel):
    """Agents suggest that ≥2 open tickets be grouped under one parent.
    Nothing changes until a human approves the proposal."""

    repository_uid: str = Field(min_length=1)
    title: str = Field(
        min_length=1,
        description="Short title for the epic — becomes the parent ticket's title on approval.",
    )
    rationale: str = Field(
        "",
        description=(
            "ONE sentence on why these ship together. Rendered on a single "
            "line in the review list and TRUNCATED past ~240 characters — put "
            "the specifics in `evidence`, not here."
        ),
    )
    member_ticket_uids: list[str] = Field(
        min_length=2,
        description="Uids of the 2-6 existing tickets that should ship together.",
    )
    axis: str = Field(
        "root-cause",
        description=(
            "What makes these belong together. One of: 'root-cause' (one "
            "defect showing up in several places, so one fix removes every "
            "symptom), 'theme' (one feature or user-facing capability — "
            "different fixes, one coherent piece of work), 'co-change' "
            "(unrelated problems whose fixes land in the same code, so one PR "
            "is cheaper and separate branches would conflict). Anything else "
            "is stored as 'root-cause': the platform computes area/feature/"
            "files/lens/class/linked groupings itself, exactly and without a "
            "model, so those are not yours to claim."
        ),
    )
    evidence: dict = Field(
        default_factory=dict,
        description=(
            "Small structured object supporting the claim, e.g. "
            '{"root_cause": "callers must remember to await close()", '
            '"shared_paths": ["a/b.py"]}. Rendered as the epic\'s one-line '
            "reason, so keep it short and put the identifying fact first."
        ),
    )
    suggested_labels: list[str] = Field(default_factory=list)
    suggested_priority: str = "medium"


@router.post(
    "/propose-group",
    operation_id="opensweep_platform_propose_epic",
)
async def platform_propose_ticket_group(
    req: PlatformProposeEpicRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
) -> dict:
    """Agents propose a ticket grouping — a human approves or rejects it.
    Approval creates a parent ticket (origin agent-proposal, backlog) with the
    members as subtickets; the members themselves are never touched here.
    Idempotent on the member set."""
    from domains.tickets.services.epic_service import EpicService

    await require_tool_repo_access(request, user, req.repository_uid)
    run_uid = request.headers.get("X-OpenSweep-Run-Uid") or ""
    actor = run_uid or user.uid
    # Plan grouping and seeded prior art come off the RUN, never off the
    # request: the agent must not be able to claim (or forget) which plan its
    # proposals belong to, and a re-proposal check the agent supplies its own
    # input for is not a check.
    plan_uid = ""
    seeded: list[list[str]] = []
    if run_uid:
        from domains.runs.models import Run

        run = await Run.nodes.get_or_none(uid=run_uid)
        target = dict(getattr(run, "target", None) or {}) if run else {}
        plan_uid = str(target.get("plan_uid") or "")
        seeded = [list(m) for m in (target.get("seeded_cluster_members") or [])]
    proposal, deduplicated = await EpicService().propose(
        repository_uid=req.repository_uid,
        title=req.title,
        rationale=req.rationale,
        member_ticket_uids=req.member_ticket_uids,
        suggested_labels=req.suggested_labels,
        suggested_priority=req.suggested_priority,
        source_run_uid=run_uid,
        actor_uid=actor,
        axis=req.axis,
        evidence=req.evidence,
        plan_uid=plan_uid,
        seeded_cluster_members=seeded,
        origin="agent",
    )
    return {"proposal_uid": proposal.uid, "deduplicated": deduplicated}


class PlatformApproveEpicRequest(BaseModel):
    proposal_uid: str = Field(min_length=1)


@router.post(
    "/approve-group",
    operation_id="opensweep_platform_approve_epic",
)
async def platform_approve_epic(
    req: PlatformApproveEpicRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
) -> dict:
    """Materialize an agent-proposed epic — gated on repo agent autonomy.

    Like Gate 1, approving a grouping (which creates the parent ticket and
    re-parents the members) is human-only by default. When the repo has opted
    into `agent_autonomy`, the agent may approve its own proposal.
    """
    from domains.repositories.services.agent_capabilities import (
        agent_autonomy_enabled,
    )
    from domains.tickets.services.epic_service import EpicService

    svc = EpicService()
    proposal = await svc.get_node(req.proposal_uid)  # 404s if missing
    await require_tool_repo_access(request, user, proposal.repository_uid)
    if not await agent_autonomy_enabled(proposal.repository_uid):
        raise HTTPException(
            status_code=403,
            detail=(
                "agent autonomy is disabled for this repository — epic approval "
                "is a human decision. Enable it in repository settings to let "
                "agents approve their own proposals."
            ),
        )
    actor = request.headers.get("X-OpenSweep-Run-Uid") or user.uid
    dto = await svc.approve(req.proposal_uid, actor_uid=actor)
    return {
        "proposal_uid": req.proposal_uid,
        "status": dto.status,
        "created_ticket_uid": getattr(dto, "created_ticket_uid", ""),
    }


@router.get(
    "/get",
    response_model=TicketDTO,
    operation_id="opensweep_platform_get_ticket",
)
async def platform_get_ticket(
    request: Request,
    ticket_uid: str = Query(...),
    user: UserDTO = Depends(get_current_user),
):
    t = await Ticket.nodes.get_or_none(uid=ticket_uid)
    if t is None:
        raise HTTPException(status_code=404, detail="not found")
    await require_tool_repo_access(request, user, t.repository_uid)
    return ticket_to_dto(t)


@router.get(
    "/list",
    response_model=list[TicketDTO],
    operation_id="opensweep_platform_list_tickets",
)
async def platform_list_tickets(
    request: Request,
    repository_uid: str = Query(""),
    status: str = Query(""),
    origin: str = Query(""),
    parent_ticket_uid: str = Query(""),
    user: UserDTO = Depends(get_current_user),
):
    # Tenancy: listing is always repository-scoped — an empty repository_uid
    # (formerly "all repos") 404s rather than leaking across orgs.
    await require_tool_repo_access(request, user, repository_uid)
    return await TicketService().list(
        repository_uids=[repository_uid],
        status=status or None,
        origin=origin or None,
        parent_ticket_uid=parent_ticket_uid or None,
    )
