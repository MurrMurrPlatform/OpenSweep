"""ScheduledAgent routes — repo-scoped automation bindings.

CRUD + manual trigger + run history + event suggestions. Replaces the
former /investigations router.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.dependencies import get_current_user, require_role
from domains.agents.schemas import (
    CreateScheduledAgentRequest,
    ScheduledAgentDTO,
    UpdateScheduledAgentRequest,
)
from domains.agents.services import scheduled_agent_service
from domains.runs.schemas import RunDTO, RunTrigger
from domains.tenancy import org_repo_uids, require_repo_in_org
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1/scheduled-agents", tags=["scheduled-agents"])


@router.get(
    "",
    response_model=list[ScheduledAgentDTO],
    operation_id="opensweep_list_scheduled_agents",
)
async def list_scheduled_agents(
    repository_uid: Optional[str] = Query(None),
    user: UserDTO = Depends(get_current_user),
):
    if repository_uid:
        await require_repo_in_org(repository_uid, user.org_uid)
        return await scheduled_agent_service.list_scheduled_agents(
            repository_uid=repository_uid
        )
    allowed = await org_repo_uids(user.org_uid)
    rows = await scheduled_agent_service.list_scheduled_agents()
    return [s for s in rows if s.repository_uid in allowed]


@router.get(
    "/{uid}",
    response_model=ScheduledAgentDTO,
    operation_id="opensweep_get_scheduled_agent",
)
async def get_scheduled_agent(uid: str, user: UserDTO = Depends(get_current_user)):
    s = await scheduled_agent_service.get_scheduled_agent_model(uid)
    await require_repo_in_org(s.repository_uid, user.org_uid)
    return await scheduled_agent_service.to_dto(s)


@router.post(
    "",
    response_model=ScheduledAgentDTO,
    status_code=201,
    operation_id="opensweep_create_scheduled_agent",
)
async def create_scheduled_agent(
    req: CreateScheduledAgentRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    await require_repo_in_org(req.repository_uid, user.org_uid)
    return await scheduled_agent_service.create_scheduled_agent(
        req, actor_uid=user.uid
    )


@router.patch(
    "/{uid}",
    response_model=ScheduledAgentDTO,
    operation_id="opensweep_update_scheduled_agent",
)
async def update_scheduled_agent(
    uid: str,
    req: UpdateScheduledAgentRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    s = await scheduled_agent_service.get_scheduled_agent_model(uid)
    await require_repo_in_org(s.repository_uid, user.org_uid)
    return await scheduled_agent_service.update_scheduled_agent(
        uid, req, actor_uid=user.uid
    )


@router.delete("/{uid}", status_code=204)
async def delete_scheduled_agent(
    uid: str, user: UserDTO = Depends(require_role("maintainer"))
):
    s = await scheduled_agent_service.get_scheduled_agent_model(uid)
    await require_repo_in_org(s.repository_uid, user.org_uid)
    await scheduled_agent_service.delete_scheduled_agent(uid, actor_uid=user.uid)


class TriggerRunRequest(BaseModel):
    trigger: RunTrigger = RunTrigger.MANUAL


@router.post(
    "/{uid}/trigger",
    response_model=RunDTO,
    operation_id="opensweep_trigger_scheduled_agent",
)
async def trigger_scheduled_agent_run(
    uid: str,
    req: TriggerRunRequest | None = None,
    user: UserDTO = Depends(require_role("maintainer")),
):
    from domains.agents.services.dispatch import trigger_scheduled_agent
    from domains.runs.services.lifecycle import LifecycleError
    from domains.runs.services.turn_service import run_to_dto

    s = await scheduled_agent_service.get_scheduled_agent_model(uid)
    await require_repo_in_org(s.repository_uid, user.org_uid)
    try:
        run = await trigger_scheduled_agent(
            uid,
            trigger=(req.trigger if req else RunTrigger.MANUAL),
            triggered_by=user.uid,
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return run_to_dto(run)


@router.get("/{uid}/runs", response_model=list[RunDTO])
async def list_runs(uid: str, user: UserDTO = Depends(get_current_user)):
    from domains.runs.models import Run
    from domains.runs.services.run_reconciliation import reconcile_stale_runs
    from domains.runs.services.turn_service import run_to_dto

    s = await scheduled_agent_service.get_scheduled_agent_model(uid)
    await require_repo_in_org(s.repository_uid, user.org_uid)
    await reconcile_stale_runs()
    nodes = await Run.nodes.all()
    out = [run_to_dto(r) for r in nodes if r.scheduled_agent_uid == uid]
    out.sort(
        key=lambda x: x.started_at
        or x.created_at
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return out


class ChangedPathsRequest(BaseModel):
    repository_uid: str
    changed_paths: list[str] = Field(default_factory=list)


@router.post("/event-suggestions")
async def event_suggestions(
    req: ChangedPathsRequest, user: UserDTO = Depends(get_current_user)
):
    """Surface ScheduledAgents eligible after a set of changed paths."""
    from domains.agents.services import event_triggers

    await require_repo_in_org(req.repository_uid, user.org_uid)
    sugg = await event_triggers.candidates_for_change(
        repository_uid=req.repository_uid,
        changed_paths=req.changed_paths,
    )
    return [
        {
            "scheduled_agent_uid": s.scheduled_agent_uid,
            "scheduled_agent_title": s.scheduled_agent_title,
            "matched_paths": s.matched_paths,
            "reason": s.reason,
            "estimates": s.estimates,
        }
        for s in sugg
    ]
