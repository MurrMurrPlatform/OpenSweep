"""Interest routes — the user-entered topics the news scout watches."""

from fastapi import APIRouter, Depends, Query

from api.dependencies import get_current_user, require_role
from domains.news.schemas import (
    CreateInterestRequest,
    InterestDTO,
    UpdateInterestRequest,
)
from domains.news.services.interest_service import InterestService
from domains.tenancy import require_repo_in_org
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1/interests", tags=["interests"])


@router.get("", response_model=list[InterestDTO], operation_id="opensweep_list_interests")
async def list_interests(
    repository_uid: str = Query(...),
    user: UserDTO = Depends(get_current_user),
):
    await require_repo_in_org(repository_uid, user.org_uid)
    return await InterestService().list(repository_uids=[repository_uid])


@router.post("", response_model=InterestDTO, operation_id="opensweep_create_interest")
async def create_interest(
    req: CreateInterestRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    await require_repo_in_org(req.repository_uid, user.org_uid)
    return await InterestService().create(req, actor_uid=user.uid)


@router.patch("/{uid}", response_model=InterestDTO, operation_id="opensweep_update_interest")
async def update_interest(
    uid: str,
    req: UpdateInterestRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    service = InterestService()
    i = await service.get_node(uid)
    await require_repo_in_org(i.repository_uid, user.org_uid)
    return await service.update(uid, req, actor_uid=user.uid)


@router.delete("/{uid}", status_code=204, operation_id="opensweep_delete_interest")
async def delete_interest(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    service = InterestService()
    i = await service.get_node(uid)
    await require_repo_in_org(i.repository_uid, user.org_uid)
    await service.delete(uid, actor_uid=user.uid)
