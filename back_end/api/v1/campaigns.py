"""Campaign routes — plan, launch, cancel, read.

Creation plans only (status=planning); launch is the separate, explicit go
signal — the celery tick then dispatches parts. Repo-scoped reads/creates
follow the standard tenancy guard; per-campaign routes resolve the
repository through the node.
"""

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from api.dependencies import get_current_user, require_role
from domains.campaigns.schemas import (
    CampaignAreasPreview,
    CampaignDTO,
    CreateCampaignRequest,
)
from domains.campaigns.services import campaign_service
from domains.tenancy import require_repo_in_org
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1", tags=["campaigns"])


@router.post(
    "/repositories/{repository_uid}/campaigns",
    response_model=CampaignDTO,
    operation_id="opensweep_campaign_create",
)
async def create_campaign(
    repository_uid: str,
    req: CreateCampaignRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    await require_repo_in_org(repository_uid, user.org_uid)
    c = await campaign_service.create(
        repository_uid, req, created_by=user.uid, trigger_provenance="manual"
    )
    return campaign_service.to_dto(c)


@router.get(
    "/repositories/{repository_uid}/campaigns",
    response_model=list[CampaignDTO],
    operation_id="opensweep_campaign_list",
)
async def list_campaigns(
    repository_uid: str, user: UserDTO = Depends(get_current_user)
):
    await require_repo_in_org(repository_uid, user.org_uid)
    return [
        campaign_service.to_dto(c)
        for c in await campaign_service.list_for_repo(repository_uid)
    ]


@router.post(
    "/repositories/{repository_uid}/campaign-plan-preview",
    operation_id="opensweep_campaign_plan_preview",
)
async def preview_campaign_plan(
    repository_uid: str,
    req: CreateCampaignRequest,
    user: UserDTO = Depends(get_current_user),
):
    """The plan a campaign WOULD produce — computed live, nothing persisted.
    Returns total_runs, by_kind, areas, uncovered_files, oversized, degraded,
    and source so the new-campaign dialog can show a live run-count preview."""
    await require_repo_in_org(repository_uid, user.org_uid)
    return await campaign_service.preview_plan(repository_uid, req)


@router.get(
    "/repositories/{repository_uid}/campaign-areas",
    response_model=CampaignAreasPreview,
    operation_id="opensweep_campaign_areas_preview",
)
async def preview_campaign_areas(
    repository_uid: str,
    area_prefix: str = Query(""),
    user: UserDTO = Depends(get_current_user),
):
    """The partition a campaign would use — computed live, nothing persisted.
    `area_prefix` slices the listing exactly as a campaign planned with it
    would."""
    await require_repo_in_org(repository_uid, user.org_uid)
    return await campaign_service.preview_areas(
        repository_uid, area_prefix=area_prefix.strip()
    )


@router.get(
    "/campaigns/{uid}",
    response_model=CampaignDTO,
    operation_id="opensweep_campaign_get",
)
async def get_campaign(uid: str, user: UserDTO = Depends(get_current_user)):
    c = await campaign_service.get(uid)
    await require_repo_in_org(c.repository_uid, user.org_uid)
    return campaign_service.to_dto(c)


@router.post(
    "/campaigns/{uid}/launch",
    response_model=CampaignDTO,
    operation_id="opensweep_campaign_launch",
)
async def launch_campaign(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    c = await campaign_service.get(uid)
    await require_repo_in_org(c.repository_uid, user.org_uid)
    return campaign_service.to_dto(await campaign_service.launch(uid, actor_uid=user.uid))


@router.delete(
    "/campaigns/{uid}",
    status_code=204,
    operation_id="opensweep_campaign_delete",
)
async def delete_campaign(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    """Delete the campaign record. 409 while running/finalizing — cancel
    first. Child runs are kept (they are the audit record)."""
    c = await campaign_service.get(uid)
    await require_repo_in_org(c.repository_uid, user.org_uid)
    await campaign_service.delete(uid, actor_uid=user.uid)
    return Response(status_code=204)


class CancelCampaignRequest(BaseModel):
    reason: str = ""


@router.post(
    "/campaigns/{uid}/cancel",
    response_model=CampaignDTO,
    operation_id="opensweep_campaign_cancel",
)
async def cancel_campaign(
    uid: str,
    req: CancelCampaignRequest | None = None,
    user: UserDTO = Depends(require_role("maintainer")),
):
    c = await campaign_service.get(uid)
    await require_repo_in_org(c.repository_uid, user.org_uid)
    return campaign_service.to_dto(
        await campaign_service.cancel(
            uid, reason=(req.reason if req else ""), actor_uid=user.uid
        )
    )
