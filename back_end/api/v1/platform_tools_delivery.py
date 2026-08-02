"""HTTP transport for the delivery platform tools (PLATFORM_V2_DESIGN.md §11).

Executor-facing surface for review/fix runs: read convergence state, bind
findings to a PR, claim fixes, grant verification, submit SHA-bound verdicts,
and *request* waivers. Role gating at the tool layer: agents never waive or
flip blocking overrides — those verbs exist only on the human delivery API.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.dependencies import get_current_user
from api.platform_scope import require_tool_repo_access
from api.v1.comments import list_comments_for
from domains.comments.schemas import CommentDTO, CommentSubjectType
from domains.delivery.models import FindingResolution, PullRequest, pr_key
from domains.delivery.schemas import (
    FindingResolutionDTO,
    FindingVerificationDTO,
    PullRequestDTO,
    SubmitVerdictRequest,
    VerdictDTO,
    VerificationResult,
)
from domains.delivery.services.pull_request_service import (
    PullRequestService,
    pull_request_to_dto,
)
from domains.delivery.services.resolution_service import ResolutionService
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1/platform-tools/delivery", tags=["platform_tools"])


async def _pr_by_ref(
    pull_request_uid: str = "", repository_uid: str = "", github_number: int | None = None
) -> PullRequest:
    """Executors may address a PR by uid or by (repository, number)."""
    if pull_request_uid:
        return await PullRequestService().get_node(pull_request_uid)
    if repository_uid and github_number is not None:
        pr = await PullRequest.nodes.get_or_none(pr_key=pr_key(repository_uid, github_number))
        if pr is not None:
            return pr
        raise HTTPException(
            status_code=404, detail=f"PullRequest {repository_uid}#{github_number} not found"
        )
    raise HTTPException(
        status_code=422, detail="provide pull_request_uid or repository_uid + github_number"
    )


async def _resolution_or_404(resolution_uid: str) -> FindingResolution:
    """Resolutions are the tenancy anchor for fix/verify/waiver verbs."""
    r = await FindingResolution.nodes.get_or_none(uid=resolution_uid)
    if r is None:
        raise HTTPException(status_code=404, detail="not found")
    return r


@router.get(
    "/convergence-state",
    response_model=PullRequestDTO,
    operation_id="opensweep_platform_get_convergence_state",
)
async def get_convergence_state(
    request: Request,
    pull_request_uid: str = Query(""),
    repository_uid: str = Query(""),
    github_number: int | None = Query(None),
    user: UserDTO = Depends(get_current_user),
):
    """The machine-readable answer to "is this PR done?" (§5)."""
    pr = await _pr_by_ref(pull_request_uid, repository_uid, github_number)
    await require_tool_repo_access(request, user, pr.repository_uid)
    return pull_request_to_dto(pr)


@router.get(
    "/resolutions",
    response_model=list[FindingResolutionDTO],
    operation_id="opensweep_platform_list_pr_resolutions",
)
async def list_pr_resolutions(
    request: Request,
    pull_request_uid: str = Query(""),
    repository_uid: str = Query(""),
    github_number: int | None = Query(None),
    user: UserDTO = Depends(get_current_user),
):
    pr = await _pr_by_ref(pull_request_uid, repository_uid, github_number)
    await require_tool_repo_access(request, user, pr.repository_uid)
    return await ResolutionService().list_for_pr(pr.uid)


class BindFindingRequest(BaseModel):
    finding_uid: str
    pull_request_uid: str = ""
    repository_uid: str = ""
    github_number: int | None = None
    introduced_at_sha: str = ""


@router.post(
    "/bind-finding",
    response_model=FindingResolutionDTO,
    operation_id="opensweep_platform_bind_finding_to_pr",
)
async def bind_finding(
    req: BindFindingRequest, request: Request, user: UserDTO = Depends(get_current_user)
):
    """Review-runs bind every finding they file to the PR+SHA under review."""
    pr = await _pr_by_ref(req.pull_request_uid, req.repository_uid, req.github_number)
    await require_tool_repo_access(request, user, pr.repository_uid)
    service = ResolutionService()
    r = await service.ensure(
        finding_uid=req.finding_uid,
        pull_request_uid=pr.uid,
        introduced_at_sha=req.introduced_at_sha,
    )
    await PullRequestService().recompute_and_publish(pr)
    dtos = await service.list_for_pr(pr.uid)
    return next(d for d in dtos if d.uid == r.uid)


class ToolAttachFixRequest(BaseModel):
    resolution_uid: str
    sha: str = Field(min_length=7)


@router.post(
    "/attach-fix",
    response_model=FindingResolutionDTO,
    operation_id="opensweep_platform_attach_fix",
)
async def attach_fix(
    req: ToolAttachFixRequest, request: Request, user: UserDTO = Depends(get_current_user)
):
    """Fixer claims fixed(sha) — verification stays with review runs (§4)."""
    resolution = await _resolution_or_404(req.resolution_uid)
    await require_tool_repo_access(request, user, resolution.repository_uid)
    service = ResolutionService()
    actor = request.headers.get("X-OpenSweep-Run-Uid") or user.uid
    r = await service.attach_fix(req.resolution_uid, sha=req.sha, actor_uid=actor)
    pr = await PullRequestService().get_node(r.pull_request_uid)
    await PullRequestService().recompute_and_publish(pr)
    dtos = await service.list_for_pr(pr.uid)
    return next(d for d in dtos if d.uid == r.uid)


class ToolVerifyRequest(BaseModel):
    resolution_uid: str
    sha: str = Field(min_length=7)
    run_uid: str = ""


@router.post(
    "/verify-resolution",
    response_model=FindingResolutionDTO,
    operation_id="opensweep_platform_verify_resolution",
)
async def verify_resolution(
    req: ToolVerifyRequest, request: Request, user: UserDTO = Depends(get_current_user)
):
    """Review/verification run grants verified at the SHA it inspected."""
    resolution = await _resolution_or_404(req.resolution_uid)
    await require_tool_repo_access(request, user, resolution.repository_uid)
    service = ResolutionService()
    run_uid = req.run_uid or request.headers.get("X-OpenSweep-Run-Uid") or ""
    r = await service.verify(req.resolution_uid, sha=req.sha, run_uid=run_uid, actor_uid=run_uid or user.uid)
    pr = await PullRequestService().get_node(r.pull_request_uid)
    await PullRequestService().recompute_and_publish(pr)
    dtos = await service.list_for_pr(pr.uid)
    return next(d for d in dtos if d.uid == r.uid)


class ToolRequestWaiverRequest(BaseModel):
    resolution_uid: str
    reason: str = Field(min_length=5)


@router.post(
    "/request-waiver",
    response_model=FindingResolutionDTO,
    operation_id="opensweep_platform_request_waiver",
)
async def request_waiver(
    req: ToolRequestWaiverRequest, request: Request, user: UserDTO = Depends(get_current_user)
):
    """Agents may request a waiver; the request lands in Needs-You (§11)."""
    resolution = await _resolution_or_404(req.resolution_uid)
    await require_tool_repo_access(request, user, resolution.repository_uid)
    service = ResolutionService()
    actor = request.headers.get("X-OpenSweep-Run-Uid") or user.uid
    r = await service.request_waiver(req.resolution_uid, reason=req.reason, actor_uid=actor)
    pr = await PullRequestService().get_node(r.pull_request_uid)
    dtos = await service.list_for_pr(pr.uid)
    return next(d for d in dtos if d.uid == r.uid)


class ToolSubmitVerdictRequest(SubmitVerdictRequest):
    pull_request_uid: str = ""
    repository_uid: str = ""
    github_number: int | None = None


@router.post(
    "/submit-verdict",
    response_model=VerdictDTO,
    operation_id="opensweep_platform_submit_verdict",
)
async def submit_verdict(
    req: ToolSubmitVerdictRequest, request: Request, user: UserDTO = Depends(get_current_user)
):
    """SHA-bound verdict — a push after this automatically stales it (§5.2)."""
    pr = await _pr_by_ref(req.pull_request_uid, req.repository_uid, req.github_number)
    await require_tool_repo_access(request, user, pr.repository_uid)
    body = SubmitVerdictRequest(
        sha=req.sha,
        result=req.result,
        new_blocking_findings=req.new_blocking_findings,
        finding_uids=req.finding_uids,
        ac_results=req.ac_results,
        source_run_uid=req.source_run_uid or request.headers.get("X-OpenSweep-Run-Uid") or "",
        executor=req.executor,
    )
    return await PullRequestService().submit_verdict(pr.uid, body, actor_uid=user.uid)


class ToolSubmitFindingVerificationRequest(BaseModel):
    finding_uid: str
    pull_request_uid: str = ""
    repository_uid: str = ""
    github_number: int | None = None
    verdict_uid: str = ""
    sha: str = Field(min_length=7)
    result: VerificationResult
    reasoning: str = Field(min_length=20)


@router.post(
    "/submit-finding-verification",
    response_model=FindingVerificationDTO,
    operation_id="opensweep_platform_submit_finding_verification",
)
async def submit_finding_verification(
    req: ToolSubmitFindingVerificationRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    """Verification runs report one judgment per finding (§A). Idempotent per
    (run, finding) — re-calls update. No recompute here; the playbook
    finalizer folds the batch into the ledger and adjusts the verdict.

    A campaign verification has no PR and no verdict — only findings to judge
    — so both are optional when the calling run carries a `campaign_uid`.
    They stay REQUIRED on the PR path: a report that cannot be attached to the
    verdict it adjusts is worse than no report, because the fold treats a
    missing report as `confirmed`."""
    from domains.delivery.services.verification_run_service import (
        submit_finding_verification as record,
    )
    from domains.runs.models import Run

    run_uid = request.headers.get("X-OpenSweep-Run-Uid") or ""
    run = await Run.nodes.get_or_none(uid=run_uid) if run_uid else None
    target = dict((run.target if run else None) or {})
    campaign_uid = str(target.get("campaign_uid") or "")

    if campaign_uid:
        if not run_uid:
            raise HTTPException(
                status_code=422,
                detail="verification reports need a run context (X-OpenSweep-Run-Uid)",
            )
        repository_uid = req.repository_uid or (run.repository_uid if run else "")
        await require_tool_repo_access(request, user, repository_uid)
        pull_request_uid, verdict_uid = "", ""
    else:
        pr = await _pr_by_ref(req.pull_request_uid, req.repository_uid, req.github_number)
        await require_tool_repo_access(request, user, pr.repository_uid)
        repository_uid = pr.repository_uid
        pull_request_uid = pr.uid
        verdict_uid = req.verdict_uid or str(target.get("verdict_uid") or "")
        if not run_uid or not verdict_uid:
            raise HTTPException(
                status_code=422,
                detail=(
                    "verification reports need a run context "
                    "(X-OpenSweep-Run-Uid) and a verdict_uid"
                ),
            )

    row = await record(
        run_uid=run_uid,
        verdict_uid=verdict_uid,
        pull_request_uid=pull_request_uid,
        repository_uid=repository_uid,
        finding_uid=req.finding_uid,
        sha=req.sha,
        result=req.result.value,
        reasoning=req.reasoning,
    )
    return FindingVerificationDTO(
        uid=row.uid,
        pull_request_uid=row.pull_request_uid,
        repository_uid=row.repository_uid,
        verdict_uid=row.verdict_uid,
        finding_uid=row.finding_uid,
        run_uid=row.run_uid,
        sha=row.sha or "",
        result=VerificationResult(row.result),
        reasoning=row.reasoning or "",
        created_at=row.created_at,
    )


@router.get(
    "/merge-policy",
    operation_id="opensweep_platform_get_merge_policy",
)
async def get_merge_policy_tool(
    request: Request,
    repository_uid: str = Query(...),
    user: UserDTO = Depends(get_current_user),
) -> dict:
    """Review-runs read the policy so new_blocking_findings matches what
    the predicate will compute."""
    from domains.delivery.services.resolution_service import ensure_merge_policy, merge_policy_to_dto

    await require_tool_repo_access(request, user, repository_uid)
    return merge_policy_to_dto(await ensure_merge_policy(repository_uid)).model_dump()


@router.get(
    "/queue",
    response_model=list[PullRequestDTO],
    operation_id="opensweep_platform_list_open_pull_requests",
)
async def list_open_pull_requests(
    request: Request,
    repository_uid: str = Query(""),
    user: UserDTO = Depends(get_current_user),
):
    # Tenancy: the queue is always repository-scoped — an empty repository_uid
    # (formerly "all repos") 404s rather than leaking across orgs.
    await require_tool_repo_access(request, user, repository_uid)
    return await PullRequestService().list(
        repository_uids=[repository_uid], state="open"
    )


@router.get(
    "/comments",
    operation_id="opensweep_platform_list_comments",
)
async def platform_list_comments(
    request: Request,
    subject_type: CommentSubjectType = Query(...),
    subject_uid: str = Query(..., min_length=1),
    user: UserDTO = Depends(get_current_user),
) -> list[CommentDTO]:
    """The discussion thread on any data item (finding, ticket, PR, news
    item, run, investigation, doc).

    Review/fix runs check this BEFORE deciding what to do about a finding —
    a maintainer's comment outranks the agent's own judgment. Reply with
    `opensweep_platform_add_comment`."""
    from domains.comments.subjects import get_subject

    subject = await get_subject(subject_type, subject_uid)
    if subject is None:
        raise HTTPException(status_code=404, detail="not found")
    await require_tool_repo_access(request, user, subject.repository_uid)
    return await list_comments_for(subject_type, subject_uid)
