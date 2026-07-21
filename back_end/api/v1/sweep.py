"""Two-stage sweep routes.

POST /api/v1/repositories/{uid}/sweep/generate-docs
    Dispatches one LLM run that proposes the documentation page tree via
    propose_doc_edit. Output lands as pending DocEdits. No per-page
    fan-out.

POST /api/v1/repositories/{uid}/sweep/audit
    Dispatches one scoped audit run per selected doc page. Focus lives
    in the optional custom_intent text, not a category picker.

See domains.runs.services.sweep for the orchestration logic.
"""


from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from api.dependencies import get_current_user, require_role
from domains.runs.schemas import Effort, normalize_effort
from domains.runs.services.active_runs import active_runs_for, conflict_detail
from domains.run_policies.services.effort import ensure_policy_for_effort
from domains.runs.services.lifecycle import LifecycleError
from domains.runs.services.sweep import (
    AuditResult,
    DeepScanResult,
    GenerateDocsResult,
    estimate_sweep_cost,
    run_audit,
    run_auto_audit,
    run_deep_scan,
    run_generate_docs,
)
from domains.tenancy import require_repo_in_org
from domains.users.schemas import UserDTO
from infrastructure.kill_switch import KillSwitchActiveError, assert_runnable

router = APIRouter(prefix="/api/v1/repositories", tags=["sweep"])


class GenerateDocsResultDTO(BaseModel):
    repository_uid: str
    run_uid: str = ""
    errors: list[str] = Field(default_factory=list)
    summary: str = ""


class GenerateDocsRequest(BaseModel):
    agent_uid: str | None = None


class AuditRequest(BaseModel):
    doc_uids: list[str] = Field(default_factory=list)
    # Staleness-driven selection (§F): pick the stalest / never-checked pages
    # automatically instead of naming doc_uids. Mutually exclusive with them.
    auto_select: bool = False
    limit: int = Field(default=3, ge=1, le=20)
    agent_uid: str | None = None
    custom_intent: str | None = None
    # Numeric findings budget per dispatched run (intent-level cap).
    max_findings: int | None = Field(default=None, ge=1, le=50)
    # Compute dial: resolves to the run policy applied to the dispatched
    # repository-scoped audit run (whole-repo path).
    effort: Effort = Effort.NORMAL

    @field_validator("effort", mode="before")
    @classmethod
    def _normalize_effort(cls, v):
        if v is None:
            return v
        return normalize_effort(v if isinstance(v, str) else (v.value if v else ""))


class AuditResultDTO(BaseModel):
    repository_uid: str
    doc_count: int
    runs_dispatched: list[str] = Field(default_factory=list)
    skipped_docs: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    summary: str = ""
    # Auto-selection provenance: [{doc_uid, slug, reason}].
    selected: list[dict] = Field(default_factory=list)


@router.post(
    "/{repository_uid}/sweep/generate-docs",
    response_model=GenerateDocsResultDTO,
    operation_id="opensweep_run_generate_docs",
)
async def run_generate_docs_endpoint(
    repository_uid: str,
    req: GenerateDocsRequest | None = None,
    user: UserDTO = Depends(require_role("maintainer")),
) -> GenerateDocsResultDTO:
    await require_repo_in_org(repository_uid, user.org_uid)
    try:
        await assert_runnable(repository_uid)
    except KillSwitchActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # In-flight guard: one generate-docs per repository at a time — a second
    # run would double-propose the same page tree. These runs carry the
    # generate-docs system agent's uid as their agent provenance.
    from domains.agents.services.registry import system_agent_by_key

    gen_agent = await system_agent_by_key("generate-docs")
    candidates = await active_runs_for(repository_uid=repository_uid)
    in_flight = [
        r
        for r in candidates
        if gen_agent is not None and (r.agent_uid or "") == gen_agent.uid
    ]
    if in_flight:
        raise HTTPException(
            status_code=409,
            detail=conflict_detail(
                "a generate-docs run is already in progress for this repository",
                in_flight[0],
            ),
        )

    try:
        result: GenerateDocsResult = await run_generate_docs(
            repository_uid=repository_uid,
            triggered_by=user.uid,
            agent_uid=req.agent_uid if req else None,
        )
    except LifecycleError as exc:
        # The docs gate (no area map yet) — a precondition conflict, not a
        # server error.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return GenerateDocsResultDTO(**result.__dict__)


@router.post(
    "/{repository_uid}/sweep/audit",
    response_model=AuditResultDTO,
    operation_id="opensweep_run_audit",
)
async def run_audit_endpoint(
    repository_uid: str,
    req: AuditRequest,
    user: UserDTO = Depends(require_role("maintainer")),
) -> AuditResultDTO:
    await require_repo_in_org(repository_uid, user.org_uid)
    try:
        await assert_runnable(repository_uid)
    except KillSwitchActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if req.auto_select and req.doc_uids:
        raise HTTPException(
            status_code=422,
            detail="auto_select and explicit doc_uids are mutually exclusive",
        )
    policy = await ensure_policy_for_effort(req.effort)
    if req.auto_select:
        result: AuditResult = await run_auto_audit(
            repository_uid=repository_uid,
            limit=req.limit,
            triggered_by=user.uid,
            agent_uid=req.agent_uid,
            custom_intent=req.custom_intent,
            max_findings=req.max_findings,
            run_policy_uid=policy.uid,
            effort=req.effort.value,
        )
        return AuditResultDTO(**result.__dict__)

    # Empty doc_uids = whole-repository audit (one repo-scoped ask run).
    result = await run_audit(
        repository_uid=repository_uid,
        doc_uids=req.doc_uids,
        triggered_by=user.uid,
        agent_uid=req.agent_uid,
        custom_intent=req.custom_intent,
        max_findings=req.max_findings,
        run_policy_uid=policy.uid,
        effort=req.effort.value,
    )
    return AuditResultDTO(**result.__dict__)


class DeepScanRequest(BaseModel):
    agent_uid: str | None = None
    # Optional focus/override text and a whole-scan findings cap.
    custom_intent: str | None = None
    max_findings: int | None = Field(default=None, ge=1, le=200)
    # Compute dial → run policy. Deep by default: a whole-repo sweep needs a
    # generous wall ceiling.
    effort: Effort = Effort.DEEP

    @field_validator("effort", mode="before")
    @classmethod
    def _normalize_effort(cls, v):
        if v is None:
            return v
        return normalize_effort(v if isinstance(v, str) else (v.value if v else ""))


class DeepScanResultDTO(BaseModel):
    repository_uid: str
    run_uid: str = ""
    errors: list[str] = Field(default_factory=list)
    summary: str = ""


@router.post(
    "/{repository_uid}/sweep/deep-scan",
    response_model=DeepScanResultDTO,
    operation_id="opensweep_run_deep_scan",
)
async def run_deep_scan_endpoint(
    repository_uid: str,
    req: DeepScanRequest | None = None,
    user: UserDTO = Depends(require_role("maintainer")),
) -> DeepScanResultDTO:
    await require_repo_in_org(repository_uid, user.org_uid)
    try:
        await assert_runnable(repository_uid)
    except KillSwitchActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # In-flight guard: one deep scan per repository at a time — a second would
    # duplicate a long, expensive whole-repo sweep. Deep-scan runs carry the
    # deep-scan system agent's uid as their agent provenance.
    from domains.agents.services.registry import system_agent_by_key

    scan_agent = await system_agent_by_key("deep-scan")
    candidates = await active_runs_for(repository_uid=repository_uid)
    in_flight = [
        r
        for r in candidates
        if scan_agent is not None and (r.agent_uid or "") == scan_agent.uid
    ]
    if in_flight:
        raise HTTPException(
            status_code=409,
            detail=conflict_detail(
                "a deep scan is already in progress for this repository",
                in_flight[0],
            ),
        )

    req = req or DeepScanRequest()
    policy = await ensure_policy_for_effort(req.effort)
    result: DeepScanResult = await run_deep_scan(
        repository_uid=repository_uid,
        triggered_by=user.uid,
        agent_uid=req.agent_uid,
        custom_intent=req.custom_intent,
        max_findings=req.max_findings,
        run_policy_uid=policy.uid,
        effort=req.effort.value,
    )
    return DeepScanResultDTO(**result.__dict__)


@router.get("/{repository_uid}/sweep/estimate")
async def sweep_estimate(
    repository_uid: str, user: UserDTO = Depends(get_current_user)
):
    from domains.docs.models import Doc

    await require_repo_in_org(repository_uid, user.org_uid)
    n = sum(
        1 for d in await Doc.nodes.all() if d.repository_uid == repository_uid
    )
    return estimate_sweep_cost(n)
