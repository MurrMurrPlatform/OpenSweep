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


@router.post(
    "/create",
    response_model=TicketDTO,
    operation_id="opensweep_platform_create_ticket",
)
async def platform_create_ticket(
    req: PlatformCreateTicketRequest, request: Request, user: UserDTO = Depends(get_current_user)
):
    """Agents propose work: origin is forced to agent-proposal and the ticket
    lands in backlog — never todo (Gate 1 is human-only)."""
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
            "What makes these belong together. Agents should send "
            "'root-cause' — the platform computes area/feature/files/lens/"
            "class/linked groupings itself, exactly, without a model."
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
        origin="agent",
    )
    return {"proposal_uid": proposal.uid, "deduplicated": deduplicated}


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
        repository_uid=repository_uid,
        status=status or None,
        origin=origin or None,
        parent_ticket_uid=parent_ticket_uid or None,
    )
