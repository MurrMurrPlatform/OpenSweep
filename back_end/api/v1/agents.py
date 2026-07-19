"""Agent routes — the org's agent library.

CRUD over user agents, org overrides (+ revisions/revert/preview) of shared
system agents, ECC re-import, and ad-hoc dispatch of an agent on a
repository. Replaces the former /agent-prompts and /agent-overlays routers.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.dependencies import get_current_user, require_platform_admin, require_role
from domains.agents.schemas import (
    AgentDTO,
    AgentDispatchRequest,
    AgentRevisionDTO,
    CreateAgentRequest,
    ImportEccResult,
    PreviewOverrideRequest,
    RevertRequest,
    SaveOverrideRequest,
    UpdateAgentRequest,
)
from domains.agents.services import agent_service
from domains.agents.services.composition import preview_composed_prompt
from domains.agents.services.registry import agent_key
from domains.runs.schemas import RunDTO
from domains.tenancy import require_repo_in_org
from domains.users.schemas import UserDTO, role_at_least

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


@router.get("", response_model=list[AgentDTO], operation_id="opensweep_list_agents")
async def list_agents(
    tag: Optional[str] = Query(None),
    provenance: Optional[str] = Query(None),
    produces: Optional[str] = Query(None),
    enabled_only: bool = Query(False),
    user: UserDTO = Depends(get_current_user),
):
    return await agent_service.list_agents(
        org_uid=user.org_uid or "",
        tag=tag,
        provenance=provenance,
        produces=produces,
        enabled_only=enabled_only,
    )


@router.get("/{uid}", response_model=AgentDTO, operation_id="opensweep_get_agent")
async def get_agent(uid: str, user: UserDTO = Depends(get_current_user)):
    return await agent_service.get_agent(uid, org_uid=user.org_uid or "")


@router.post("", response_model=AgentDTO, status_code=201, operation_id="opensweep_create_agent")
async def create_agent(
    req: CreateAgentRequest,
    user: UserDTO = Depends(get_current_user),
):
    return await agent_service.create_agent(
        req,
        org_uid=user.org_uid or "",
        actor_uid=user.uid,
        allow_write_produces=role_at_least(user.role, "maintainer"),
    )


@router.patch("/{uid}", response_model=AgentDTO, operation_id="opensweep_update_agent")
async def update_agent(
    uid: str,
    req: UpdateAgentRequest,
    user: UserDTO = Depends(get_current_user),
):
    return await agent_service.update_agent(
        uid,
        req,
        org_uid=user.org_uid or "",
        actor_uid=user.uid,
        allow_write_produces=role_at_least(user.role, "maintainer"),
        platform_admin=user.is_platform_admin,
    )


@router.delete("/{uid}", status_code=204)
async def delete_agent(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    await agent_service.delete_agent(uid, org_uid=user.org_uid or "", actor_uid=user.uid)


# ── Org overrides of system agents (the absorbed overlay system) ────────────


@router.put("/{uid}/override", response_model=AgentRevisionDTO)
async def save_override(
    uid: str,
    req: SaveOverrideRequest,
    user: UserDTO = Depends(get_current_user),
):
    return await agent_service.save_override(
        agent_uid=uid,
        org_uid=user.org_uid or "",
        mode=req.mode,
        body=req.body,
        enabled=req.enabled,
        actor_uid=user.uid,
    )


@router.delete("/{uid}/override", status_code=204)
async def delete_override(uid: str, user: UserDTO = Depends(get_current_user)):
    await agent_service.delete_override(
        agent_uid=uid, org_uid=user.org_uid or "", actor_uid=user.uid
    )


@router.get("/{uid}/revisions", response_model=list[AgentRevisionDTO])
async def list_revisions(uid: str, user: UserDTO = Depends(get_current_user)):
    return await agent_service.list_revisions(uid, org_uid=user.org_uid or "")


@router.post("/{uid}/revert", response_model=AgentRevisionDTO)
async def revert_override(
    uid: str,
    req: RevertRequest,
    user: UserDTO = Depends(get_current_user),
):
    return await agent_service.revert_override(
        agent_uid=uid, org_uid=user.org_uid or "", rev=req.rev, actor_uid=user.uid
    )


class PreviewResponse(BaseModel):
    prompt: str


@router.post("/{uid}/preview", response_model=PreviewResponse)
async def preview_override(
    uid: str,
    req: PreviewOverrideRequest,
    user: UserDTO = Depends(get_current_user),
):
    """The fully composed prompt for a DRAFT override (not persisted)."""
    a = await agent_service.get_agent_model(uid, org_uid=user.org_uid or "")
    key = agent_key(a.source_url or "")
    if not key:
        raise HTTPException(
            status_code=422, detail="preview applies to system agents only"
        )
    text = await preview_composed_prompt(
        org_uid=user.org_uid or "", agent_key=key, mode=req.mode, body=req.body
    )
    return PreviewResponse(prompt=text)


# ── Dispatch + import ───────────────────────────────────────────────────────


@router.post(
    "/{uid}/runs",
    response_model=RunDTO,
    operation_id="opensweep_dispatch_agent",
)
async def dispatch_agent_run(
    uid: str,
    req: AgentDispatchRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    """Ad-hoc run of an agent on a repository (no schedule needed)."""
    from domains.agents.services.dispatch import dispatch_agent
    from domains.runs.services.lifecycle import LifecycleError
    from domains.runs.services.turn_service import run_to_dto

    await require_repo_in_org(req.repository_uid, user.org_uid)
    a = await agent_service.get_agent_model(uid, org_uid=user.org_uid or "")
    try:
        run = await dispatch_agent(
            agent=a,
            repository_uid=req.repository_uid,
            target=req.target,
            effort=req.effort or "",
            triggered_by=user.uid,
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return run_to_dto(run)


@router.post("/import-ecc", response_model=ImportEccResult)
async def import_ecc_endpoint(user: UserDTO = Depends(require_platform_admin)):
    from domains.agents.services.ecc_import import import_ecc

    return await import_ecc()
