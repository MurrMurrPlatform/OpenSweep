"""Ticket group proposal routes — list, approve, reject.

Agents propose groupings through the platform tool surface
(`opensweep_platform_propose_epic`); humans review them here. Approval
materializes the parent ticket and re-parents the members — mirroring the
Gate-1 contract, agents can never apply their own groupings.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import get_current_user, require_role
from domains.tenancy import org_repo_uids, require_repo_in_org
from domains.tickets.models import EPIC_PROPOSAL_STATUSES
from domains.tickets.schemas import BulkApproveRequest, EpicProposalDTO
from domains.tickets.services.epic_service import EpicService
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1/epic-proposals", tags=["tickets"])


@router.get(
    "",
    response_model=list[EpicProposalDTO],
    operation_id="opensweep_epic_proposal_list",
)
async def list_group_proposals(
    repository_uid: str | None = Query(None),
    status: str | None = Query(None),
    user: UserDTO = Depends(get_current_user),
):
    if status is not None and status not in EPIC_PROPOSAL_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {sorted(EPIC_PROPOSAL_STATUSES)}",
        )
    if repository_uid is not None:
        await require_repo_in_org(repository_uid, user.org_uid)
    items = await EpicService().list(repository_uid=repository_uid, status=status)
    if repository_uid is None:
        allowed = await org_repo_uids(user.org_uid)
        items = [p for p in items if p.repository_uid in allowed]
    return items


@router.post("/bulk-approve", operation_id="opensweep_epic_proposal_bulk_approve")
async def bulk_approve_group_proposals(
    req: BulkApproveRequest, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    """Approve a whole plan of epics in one review action.

    A rule that produces "12 tickets → 4 epics" should cost one decision,
    not four. Partial success is REPORTED rather than raised: if one epic went
    stale since it was proposed, the other three still land and the response
    names the failure. Returns `{approved: [...], errors: [{uid, detail}]}`.
    """
    service = EpicService()
    # Tenancy is checked per proposal, not once for the request: the uid list
    # is client-supplied and could otherwise mix in another org's proposal.
    for uid in req.uids:
        proposal = await service.get_node(uid)
        await require_repo_in_org(proposal.repository_uid, user.org_uid)
    return await service.bulk_approve(req.uids, actor_uid=user.uid)


@router.post(
    "/{uid}/approve",
    response_model=EpicProposalDTO,
    operation_id="opensweep_epic_proposal_approve",
)
async def approve_group_proposal(
    uid: str, user: UserDTO = Depends(require_role("maintainer"))
):
    """Approve: creates the parent ticket and passes Gate 1 on it in the same
    action (the approval IS the gate), then re-parents the still-open members.
    409 if already reviewed or fewer than 2 members remain open."""
    service = EpicService()
    proposal = await service.get_node(uid)
    await require_repo_in_org(proposal.repository_uid, user.org_uid)
    return await service.approve(uid, actor_uid=user.uid)


@router.post(
    "/{uid}/reject",
    response_model=EpicProposalDTO,
    operation_id="opensweep_epic_proposal_reject",
)
async def reject_group_proposal(
    uid: str, user: UserDTO = Depends(require_role("maintainer"))
):
    service = EpicService()
    proposal = await service.get_node(uid)
    await require_repo_in_org(proposal.repository_uid, user.org_uid)
    return await service.reject(uid, actor_uid=user.uid)
