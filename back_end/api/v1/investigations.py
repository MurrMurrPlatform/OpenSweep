"""Investigation routes — CRUD + dispatch + templates + event-trigger inbox."""

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.dependencies import get_current_user, require_role
from domains.investigations.models import Investigation, Run
from domains.investigations.schemas import (
    CreateInvestigationRequest,
    InvestigationDTO,
    InvestigationEffort,
    InvestigationProvenance,
    RunDTO,
    RunTrigger,
    UpdateInvestigationRequest,
    normalize_effort,
)
from domains.investigations.schemas import parse_schedule
from domains.investigations.services import event_triggers
from domains.investigations.services.job_types import get_job_type, list_job_types
from domains.investigations.services.lifecycle import LifecycleError, trigger_run
from domains.investigations.services.run_reconciliation import reconcile_stale_runs
from domains.investigations.services.turn_service import run_to_dto
from domains.run_policies.services.effort import ensure_policy_for_effort
from domains.tenancy import org_repo_uids, require_repo_in_org
from domains.users.schemas import UserDTO
from infrastructure.audit import write_audit

router = APIRouter(prefix="/api/v1/investigations", tags=["investigations"])


def _to_dto(i: Investigation) -> InvestigationDTO:
    return InvestigationDTO(
        uid=i.uid,
        repository_uid=i.repository_uid,
        title=i.title or "",
        description=i.description or "",
        intent=i.intent,
        job_type=i.job_type or "audit",
        target=dict(i.target or {}),
        effort=normalize_effort(i.effort),
        schedule=i.schedule or "",
        default_executor=i.default_executor or "internal_llm",
        default_mode=i.default_mode or "analyze_only",
        run_policy_uid=i.run_policy_uid,
        provenance=InvestigationProvenance(i.provenance or "human-asked"),
        compute_dial=i.compute_dial or "ask-before-run",
        created_at=i.created_at,
        updated_at=i.updated_at,
    )


@router.get("", response_model=list[InvestigationDTO], operation_id="opensweep_list_investigations")
async def list_investigations(
    repository_uid: Optional[str] = Query(None),
    user: UserDTO = Depends(get_current_user),
):
    if repository_uid:
        await require_repo_in_org(repository_uid, user.org_uid)
    allowed = await org_repo_uids(user.org_uid)
    nodes = await Investigation.nodes.all()
    out = [
        _to_dto(i)
        for i in nodes
        if i.repository_uid in allowed
        and (not repository_uid or i.repository_uid == repository_uid)
    ]
    out.sort(
        key=lambda x: x.updated_at or x.created_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return out


@router.get("/job-types")
async def list_investigation_job_types():
    return [
        {
            "job_type": jt.job_type,
            "title": jt.title,
            "description": jt.description,
            "intent": jt.intent,
        }
        for jt in list_job_types()
    ]


@router.get("/{uid}", response_model=InvestigationDTO, operation_id="opensweep_get_investigation")
async def get_investigation(uid: str, user: UserDTO = Depends(get_current_user)):
    i = await Investigation.nodes.get_or_none(uid=uid)
    if i is None:
        raise HTTPException(status_code=404, detail=f"Investigation {uid} not found")
    await require_repo_in_org(i.repository_uid, user.org_uid)
    return _to_dto(i)


@router.post(
    "",
    response_model=InvestigationDTO,
    status_code=201,
    operation_id="opensweep_create_investigation",
)
async def create_investigation(
    req: CreateInvestigationRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    await require_repo_in_org(req.repository_uid, user.org_uid)
    job_type = get_job_type(req.job_type) if req.job_type else None
    if job_type is None:
        raise HTTPException(status_code=422, detail=f"unknown job_type: {req.job_type!r}")
    intent = req.intent
    if not intent:
        # No explicit intent: compose the job type's canned intent with the
        # repo's workflow prompt for this stage (if one is configured).
        from domains.investigations.services._intent_helpers import build_intent
        from domains.repositories.services.workflow import stage_prompt_body

        stage = {
            "discover-capabilities": "discover",
            "document": "document",
            "implement": "implement",
        }.get(job_type.job_type, "ask")
        prompt_body = await stage_prompt_body(req.repository_uid, stage)
        intent = (
            build_intent(prompt_body=prompt_body, default_intent=job_type.intent)
            if prompt_body
            else job_type.intent
        )
    if not intent:
        raise HTTPException(status_code=422, detail="intent is required")
    try:
        parse_schedule(req.schedule)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid schedule: {exc}")
    policy_uid = req.run_policy_uid
    if not policy_uid:
        policy = await ensure_policy_for_effort(req.effort)
        policy_uid = policy.uid
    i = Investigation(
        uid=uuid4().hex,
        repository_uid=req.repository_uid,
        title=req.title,
        description=req.description,
        intent=intent,
        job_type=job_type.job_type,
        target=req.target,
        effort=req.effort.value,
        schedule=req.schedule,
        default_executor=req.default_executor.value,
        default_mode=req.default_mode.value,
        run_policy_uid=policy_uid,
        provenance=InvestigationProvenance.HUMAN_ASKED.value,
        compute_dial=req.compute_dial.value,
    )
    await i.save()
    await write_audit(
        kind="investigation.created",
        subject_uid=i.uid,
        subject_type="Investigation",
        actor_uid=user.uid,
        payload={"intent": i.intent, "executor": i.default_executor, "job_type": i.job_type},
    )
    return _to_dto(i)


@router.patch(
    "/{uid}",
    response_model=InvestigationDTO,
    operation_id="opensweep_update_investigation",
)
async def update_investigation(
    uid: str,
    req: UpdateInvestigationRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    """Partial update — the scheduling surface (cron, compute dial, effort,
    target.limit) plus title/description/intent. None = unchanged."""
    i = await Investigation.nodes.get_or_none(uid=uid)
    if i is None:
        raise HTTPException(status_code=404, detail=f"Investigation {uid} not found")
    await require_repo_in_org(i.repository_uid, user.org_uid)
    if req.schedule is not None:
        try:
            parse_schedule(req.schedule)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid schedule: {exc}")
        i.schedule = req.schedule
    if req.title is not None:
        i.title = req.title
    if req.description is not None:
        i.description = req.description
    if req.intent is not None:
        if not req.intent.strip():
            raise HTTPException(status_code=422, detail="intent cannot be emptied")
        i.intent = req.intent
    if req.target is not None:
        i.target = req.target
    if req.effort is not None:
        i.effort = req.effort.value
        policy = await ensure_policy_for_effort(req.effort)
        i.run_policy_uid = policy.uid
    if req.compute_dial is not None:
        i.compute_dial = req.compute_dial.value
    i.updated_at = datetime.now(timezone.utc)
    await i.save()
    await write_audit(
        kind="investigation.updated",
        subject_uid=i.uid,
        subject_type="Investigation",
        actor_uid=user.uid,
        payload=req.model_dump(exclude_none=True),
    )
    return _to_dto(i)


@router.delete("/{uid}", status_code=204)
async def delete_investigation(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    i = await Investigation.nodes.get_or_none(uid=uid)
    if i is None:
        raise HTTPException(status_code=404, detail=f"Investigation {uid} not found")
    await require_repo_in_org(i.repository_uid, user.org_uid)
    await i.delete()
    await write_audit(
        kind="investigation.deleted",
        subject_uid=uid,
        subject_type="Investigation",
        actor_uid=user.uid,
    )


class TriggerRunRequest(BaseModel):
    run_policy_uid: Optional[str] = None
    trigger: RunTrigger = RunTrigger.MANUAL


@router.post(
    "/{uid}/runs",
    response_model=RunDTO,
    operation_id="opensweep_trigger_investigation_run",
)
async def trigger_investigation_run(
    uid: str,
    req: TriggerRunRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    i = await Investigation.nodes.get_or_none(uid=uid)
    if i is None:
        raise HTTPException(status_code=404, detail=f"Investigation {uid} not found")
    await require_repo_in_org(i.repository_uid, user.org_uid)
    try:
        run = await trigger_run(
            investigation_uid=uid,
            run_policy_uid=req.run_policy_uid,
            trigger=req.trigger,
            triggered_by=user.uid,
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return run_to_dto(run)


@router.get("/{uid}/runs", response_model=list[RunDTO])
async def list_runs(uid: str, user: UserDTO = Depends(get_current_user)):
    i = await Investigation.nodes.get_or_none(uid=uid)
    if i is None:
        raise HTTPException(status_code=404, detail=f"Investigation {uid} not found")
    await require_repo_in_org(i.repository_uid, user.org_uid)
    await reconcile_stale_runs()
    nodes = await Run.nodes.all()
    out = [run_to_dto(r) for r in nodes if r.investigation_uid == uid]
    out.sort(
        key=lambda x: x.started_at or x.created_at or datetime.min.replace(tzinfo=timezone.utc),
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
    """Surface Investigations eligible after a set of changed paths."""
    await require_repo_in_org(req.repository_uid, user.org_uid)
    sugg = await event_triggers.candidates_for_change(
        repository_uid=req.repository_uid,
        changed_paths=req.changed_paths,
    )
    return [
        {
            "investigation_uid": s.investigation_uid,
            "investigation_title": s.investigation_title,
            "matched_paths": s.matched_paths,
            "reason": s.reason,
            "estimates": s.estimates,
        }
        for s in sugg
    ]
