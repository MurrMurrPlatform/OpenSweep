"""Area map API — Areas + AreaEdit review.

Humans edit Areas directly (no workflow); agent proposals arrive as
AreaEdits and are accepted/rejected here — individually or in bulk (a
mapped area tree is one decision, not fifteen). Accepting applies the
edit's full replacement (creating the area for new-area proposals) and
returns partition warnings for the human to eyeball. The map-areas sweep
dispatches one LLM run that proposes the whole tree via propose_area_edit.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.dependencies import get_current_user, require_role
from domains.areas.schemas import (
    AcceptAreaEditResponse,
    AreaDetailDTO,
    AreaDTO,
    AreaEditDTO,
    BulkAreaEditRequest,
    UpdateAreaRequest,
    UpdateAreaResponse,
)
from domains.areas.services import area_service
from domains.runs.services.active_runs import conflict_detail
from domains.tenancy import require_repo_in_org
from domains.users.schemas import UserDTO
from infrastructure.kill_switch import KillSwitchActiveError, assert_runnable

router = APIRouter(prefix="/api/v1", tags=["areas"])


@router.get("/areas", operation_id="opensweep_list_areas")
async def list_areas(
    repository_uid: str = Query(...),
    user: UserDTO = Depends(get_current_user),
) -> list[AreaDTO]:
    await require_repo_in_org(repository_uid, user.org_uid)
    return await area_service.list_areas(repository_uid)


@router.get("/areas/{uid}", operation_id="opensweep_get_area")
async def get_area(uid: str, user: UserDTO = Depends(get_current_user)) -> AreaDTO:
    a = await area_service.get_area(uid)
    await require_repo_in_org(a.repository_uid, user.org_uid)
    return area_service.area_to_dto(a)


@router.get("/areas/{uid}/detail", operation_id="opensweep_area_detail")
async def area_detail(
    uid: str, user: UserDTO = Depends(get_current_user)
) -> AreaDetailDTO:
    """Everything the area detail page needs in one load: scope sized
    against the live tree, linked/suggested docs, related areas, recent
    coverage, pending edits."""
    a = await area_service.get_area(uid)
    await require_repo_in_org(a.repository_uid, user.org_uid)
    return await area_service.area_detail(uid)


@router.patch("/areas/{uid}", operation_id="opensweep_update_area")
async def update_area(
    uid: str, req: UpdateAreaRequest, user: UserDTO = Depends(require_role("maintainer"))
) -> UpdateAreaResponse:
    existing = await area_service.get_area(uid)
    await require_repo_in_org(existing.repository_uid, user.org_uid)
    a, warnings = await area_service.update_area(uid, req, actor=user.uid)
    return UpdateAreaResponse(area=area_service.area_to_dto(a), warnings=warnings)


@router.delete("/areas/{uid}", operation_id="opensweep_delete_area")
async def delete_area(uid: str, user: UserDTO = Depends(require_role("maintainer"))) -> dict:
    a = await area_service.get_area(uid)
    await require_repo_in_org(a.repository_uid, user.org_uid)
    await area_service.delete_area(uid, actor=user.uid)
    return {"status": "deleted"}


# ---------- AreaEdits ----------


@router.get("/area-edits", operation_id="opensweep_list_area_edits")
async def list_area_edits(
    repository_uid: str = Query(...),
    status: str = "pending",
    user: UserDTO = Depends(get_current_user),
) -> list[AreaEditDTO]:
    await require_repo_in_org(repository_uid, user.org_uid)
    return await area_service.list_area_edits(repository_uid, status=status)


async def _require_area_edits_in_org(uids: list[str], org_uid: str) -> None:
    """404 if ANY area-edit in the batch is outside the org (or missing)."""
    for uid in uids:
        e = await area_service.get_area_edit(uid)
        await require_repo_in_org(e.repository_uid, org_uid)


@router.post("/area-edits/{uid}/accept", operation_id="opensweep_accept_area_edit")
async def accept_area_edit(
    uid: str, user: UserDTO = Depends(require_role("maintainer"))
) -> AcceptAreaEditResponse:
    e = await area_service.get_area_edit(uid)
    await require_repo_in_org(e.repository_uid, user.org_uid)
    a, warnings = await area_service.accept_area_edit(uid, actor=user.uid)
    return AcceptAreaEditResponse(area=area_service.area_to_dto(a), warnings=warnings)


@router.post("/area-edits/{uid}/reject", operation_id="opensweep_reject_area_edit")
async def reject_area_edit(
    uid: str, user: UserDTO = Depends(require_role("maintainer"))
) -> AreaEditDTO:
    e = await area_service.get_area_edit(uid)
    await require_repo_in_org(e.repository_uid, user.org_uid)
    return await area_service.reject_area_edit(uid, actor=user.uid)


@router.post("/area-edits/bulk-accept", operation_id="opensweep_bulk_accept_area_edits")
async def bulk_accept_area_edits(
    req: BulkAreaEditRequest, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    await _require_area_edits_in_org(req.uids, user.org_uid)
    accepted: list[str] = []
    warnings: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    for uid in req.uids:
        try:
            _, edit_warnings = await area_service.accept_area_edit(uid, actor=user.uid)
            accepted.append(uid)
            if edit_warnings:
                warnings[uid] = edit_warnings
        except HTTPException as exc:
            errors[uid] = str(exc.detail)
    return {"accepted": accepted, "warnings": warnings, "errors": errors}


@router.post("/area-edits/bulk-reject", operation_id="opensweep_bulk_reject_area_edits")
async def bulk_reject_area_edits(
    req: BulkAreaEditRequest, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    await _require_area_edits_in_org(req.uids, user.org_uid)
    rejected: list[str] = []
    errors: dict[str, str] = {}
    for uid in req.uids:
        try:
            await area_service.reject_area_edit(uid, actor=user.uid)
            rejected.append(uid)
        except HTTPException as exc:
            errors[uid] = str(exc.detail)
    return {"rejected": rejected, "errors": errors}


# ---------- Map-areas sweep ----------


class MapAreasResultDTO(BaseModel):
    repository_uid: str
    run_uid: str = ""
    errors: list[str] = Field(default_factory=list)
    summary: str = ""


@router.post(
    "/repositories/{repository_uid}/areas/reset", operation_id="opensweep_reset_areas"
)
async def reset_areas(
    repository_uid: str, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    """Destructive: delete the repository's entire Area map (areas + edits).
    Coverage history stays; campaigns fall back to docs-derived planning
    until a new map is accepted."""
    await require_repo_in_org(repository_uid, user.org_uid)
    return await area_service.reset_areas(repository_uid, actor=user.uid)


@router.post(
    "/repositories/{repository_uid}/sweep/map-areas",
    response_model=MapAreasResultDTO,
    operation_id="opensweep_run_map_areas",
)
async def run_map_areas_endpoint(
    repository_uid: str,
    user: UserDTO = Depends(require_role("maintainer")),
) -> MapAreasResultDTO:
    await require_repo_in_org(repository_uid, user.org_uid)
    try:
        await assert_runnable(repository_uid)
    except KillSwitchActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Lazy import: the sweep orchestration lands separately; the router must
    # mount regardless of import order (and tests monkeypatch this seam).
    from domains.runs.services.sweep import map_areas_run_in_flight, run_map_areas

    # In-flight guard: one map-areas run per repository at a time — a second
    # run would double-propose the same area tree. The helper matches by the
    # map-areas system agent's provenance and logs when the guard is inert
    # (agent not seeded); the schedule scanner shares it.
    in_flight = await map_areas_run_in_flight(repository_uid)
    if in_flight is not None:
        raise HTTPException(
            status_code=409,
            detail=conflict_detail(
                "a map-areas run is already in progress for this repository",
                in_flight,
            ),
        )

    result = await run_map_areas(repository_uid=repository_uid, triggered_by=user.uid)
    return MapAreasResultDTO(
        repository_uid=result.repository_uid,
        run_uid=result.run_uid,
        errors=list(result.errors),
        summary=result.summary,
    )
