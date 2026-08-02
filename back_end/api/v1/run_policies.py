"""RunPolicy routes -- basic CRUD scaffold.

Policy resolution, ceiling enforcement, dry-run, and routing constraints
bound read-only tracking investigations.
"""

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import get_current_user, require_platform_admin
from domains.run_policies.models import RunPolicy
from domains.run_policies.schemas import (
    CreateRunPolicyRequest,
    RunPolicyDTO,
    UpdateRunPolicyRequest,
)
from domains.users.schemas import UserDTO
from infrastructure.audit import write_audit

router = APIRouter(prefix="/api/v1/run-policies", tags=["run_policies"])


def _to_dto(p: RunPolicy) -> RunPolicyDTO:
    return RunPolicyDTO(
        uid=p.uid,
        name=p.name or "",
        description=p.description or "",
        max_wall_seconds=p.max_wall_seconds,
        max_tool_turns=p.max_tool_turns,
        max_files_touched=p.max_files_touched,
        max_continuation_passes=p.max_continuation_passes,
        cloud_allowed=bool(p.cloud_allowed),
        local_only=bool(p.local_only),
        allowed_executors=list(p.allowed_executors or []),
        dry_run=bool(p.dry_run),
        warn_at_pct=int(p.warn_at_pct or 80),
        daily_repo_run_count=p.daily_repo_run_count,
        daily_repo_wall_seconds=p.daily_repo_wall_seconds,
        daily_repo_dollars=p.daily_repo_dollars,
        version=int(p.version or 1),
        supersedes_uid=p.supersedes_uid,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


@router.get("", response_model=list[RunPolicyDTO], operation_id="opensweep_list_run_policies")
async def list_run_policies(
    name: Optional[str] = Query(None),
    # Operator-only, matching create/update/delete. Run policies are global
    # platform config (no org_uid — every one is created by a platform admin),
    # so listing them all is an operator action; a plain get_current_user gate
    # let any authenticated tenant enumerate the instance's cost ceilings and
    # model routing. get_run_policy stays open: a run's detail view resolves the
    # single policy its own run used (and degrades to null on refusal).
    user: UserDTO = Depends(require_platform_admin),
):
    nodes = await RunPolicy.nodes.all()
    out = [_to_dto(p) for p in nodes if not name or (p.name or "") == name]
    out.sort(key=lambda x: (x.name, -x.version))
    return out


@router.get("/{uid}", response_model=RunPolicyDTO, operation_id="opensweep_get_run_policy")
async def get_run_policy(uid: str, user: UserDTO = Depends(get_current_user)):
    p = await RunPolicy.nodes.get_or_none(uid=uid)
    if p is None:
        raise HTTPException(status_code=404, detail=f"RunPolicy {uid} not found")
    return _to_dto(p)


@router.post(
    "",
    response_model=RunPolicyDTO,
    status_code=201,
    operation_id="opensweep_create_run_policy",
)
async def create_run_policy(
    req: CreateRunPolicyRequest,
    user: UserDTO = Depends(require_platform_admin),
):
    p = RunPolicy(
        uid=uuid4().hex,
        name=req.name,
        description=req.description,
        max_wall_seconds=req.max_wall_seconds,
        max_tool_turns=req.max_tool_turns,
        max_files_touched=req.max_files_touched,
        max_continuation_passes=req.max_continuation_passes,
        cloud_allowed=req.cloud_allowed,
        local_only=req.local_only,
        allowed_executors=req.allowed_executors,
        dry_run=req.dry_run,
        warn_at_pct=req.warn_at_pct,
        daily_repo_run_count=req.daily_repo_run_count,
        daily_repo_wall_seconds=req.daily_repo_wall_seconds,
        daily_repo_dollars=req.daily_repo_dollars,
    )
    await p.save()
    await write_audit(
        kind="run_policy.created",
        subject_uid=p.uid,
        subject_type="RunPolicy",
        actor_uid=user.uid,
        payload={"name": p.name},
    )
    return _to_dto(p)


@router.patch(
    "/{uid}",
    response_model=RunPolicyDTO,
    operation_id="opensweep_update_run_policy",
)
async def update_run_policy(
    uid: str,
    req: UpdateRunPolicyRequest,
    user: UserDTO = Depends(require_platform_admin),
):
    p = await RunPolicy.nodes.get_or_none(uid=uid)
    if p is None:
        raise HTTPException(status_code=404, detail=f"RunPolicy {uid} not found")

    fields = req.model_dump(exclude_unset=True)

    # Validate the *merged* routing state — partial edits can otherwise slip a
    # local_only+cloud_allowed footgun past the create-time validator.
    merged_local_only = fields.get("local_only", bool(p.local_only))
    merged_cloud_allowed = fields.get("cloud_allowed", bool(p.cloud_allowed))
    if merged_local_only and merged_cloud_allowed:
        raise HTTPException(
            status_code=422,
            detail=(
                "RunPolicy: local_only=true implies cloud_allowed=false — "
                "cannot set both at once"
            ),
        )

    for key, value in fields.items():
        setattr(p, key, value)
    p.updated_at = datetime.now(timezone.utc)
    await p.save()
    await write_audit(
        kind="run_policy.updated",
        subject_uid=p.uid,
        subject_type="RunPolicy",
        actor_uid=user.uid,
        payload={"fields": sorted(fields.keys())},
    )
    return _to_dto(p)


@router.delete("/{uid}", status_code=204)
async def delete_run_policy(uid: str, user: UserDTO = Depends(require_platform_admin)):
    p = await RunPolicy.nodes.get_or_none(uid=uid)
    if p is None:
        raise HTTPException(status_code=404, detail=f"RunPolicy {uid} not found")
    await p.delete()
    await write_audit(
        kind="run_policy.deleted",
        subject_uid=uid,
        subject_type="RunPolicy",
        actor_uid=user.uid,
    )
