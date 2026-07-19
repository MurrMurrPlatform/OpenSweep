"""Per-repo workflow config API.

GET/PUT the stage → {agent_uid, auto} mapping every domain trigger
reads (domains/repositories/services/workflow.py).
"""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.dependencies import get_current_user, require_role
from domains.repositories.services import workflow as workflow_service
from domains.tenancy import require_repo_in_org
from domains.users.schemas import UserDTO
from infrastructure.audit import write_audit

router = APIRouter(prefix="/api/v1/repositories", tags=["workflow"])


class WorkflowStageDTO(BaseModel):
    agent_uid: str = ""
    auto: bool = False
    # quick | normal | deep — recall/precision dial applied by stage triggers.
    depth: str = "normal"
    # Per-stage dispatch overrides; empty/0 = inherit platform defaults
    # (active provider chain, provider's model, run policy wall ceiling).
    provider_uid: str = ""
    model: str = ""
    max_wall_seconds: int = 0
    # Full run-policy override for this stage (dollars/wall/turns/files).
    # Empty = inherit the system default.
    run_policy_uid: str = ""


class WorkflowDTO(BaseModel):
    stages: dict[str, WorkflowStageDTO]
    # Which stages have a defined automatic trigger (UI shows toggles only here).
    auto_stages: list[str] = Field(default_factory=list)


class UpdateWorkflowRequest(BaseModel):
    stages: dict[str, WorkflowStageDTO] = Field(default_factory=dict)


def _to_dto(config: dict[str, dict[str, Any]]) -> WorkflowDTO:
    return WorkflowDTO(
        stages={stage: WorkflowStageDTO(**entry) for stage, entry in config.items()},
        auto_stages=list(workflow_service.AUTO_STAGES),
    )


@router.get("/{repository_uid}/workflow", operation_id="opensweep_get_workflow")
async def get_workflow(
    repository_uid: str, user: UserDTO = Depends(get_current_user)
) -> WorkflowDTO:
    await require_repo_in_org(repository_uid, user.org_uid)
    return _to_dto(await workflow_service.get_workflow(repository_uid))


@router.put("/{repository_uid}/workflow", operation_id="opensweep_update_workflow")
async def update_workflow(
    repository_uid: str,
    req: UpdateWorkflowRequest,
    user: UserDTO = Depends(require_role("maintainer")),
) -> WorkflowDTO:
    await require_repo_in_org(repository_uid, user.org_uid)
    config = await workflow_service.set_workflow(
        repository_uid,
        {stage: entry.model_dump() for stage, entry in req.stages.items()},
    )
    await write_audit(
        kind="workflow.updated",
        subject_uid=repository_uid,
        subject_type="Repository",
        actor_uid=user.uid,
        payload=config,
    )
    return _to_dto(config)


# ── Static-analyzer config (sibling per-repo config, §E) ─────────────────────


class AnalyzerToolDTO(BaseModel):
    tool: str
    args: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)


class AnalyzersDTO(BaseModel):
    mode: str = "auto"  # auto | custom | off
    tools: list[AnalyzerToolDTO] = Field(default_factory=list)


@router.get("/{repository_uid}/analyzers", operation_id="opensweep_get_analyzers")
async def get_analyzers(
    repository_uid: str, user: UserDTO = Depends(get_current_user)
) -> AnalyzersDTO:
    from domains.repositories.services import analyzer_config

    await require_repo_in_org(repository_uid, user.org_uid)
    return AnalyzersDTO(**await analyzer_config.get_analyzers(repository_uid))


@router.put("/{repository_uid}/analyzers", operation_id="opensweep_update_analyzers")
async def update_analyzers(
    repository_uid: str,
    req: AnalyzersDTO,
    user: UserDTO = Depends(require_role("maintainer")),
) -> AnalyzersDTO:
    from domains.repositories.services import analyzer_config

    await require_repo_in_org(repository_uid, user.org_uid)
    config = await analyzer_config.set_analyzers(repository_uid, req.model_dump())
    await write_audit(
        kind="analyzers.updated",
        subject_uid=repository_uid,
        subject_type="Repository",
        actor_uid=user.uid,
        payload=config,
    )
    return AnalyzersDTO(**config)
