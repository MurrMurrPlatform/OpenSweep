"""Finding routes — list, get, file, dismiss, acknowledge, mark fixed."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from api.dependencies import get_current_user, require_role
from domains.findings.queries import find_similar
from domains.findings.schemas import (
    FileFindingRequest,
    FindingDTO,
    UpdateFindingRequest,
)
from domains.findings.services.finding_service import (
    FINDING_SORT_DIRS,
    FINDING_SORT_FIELDS,
    FindingService,
    finding_to_dto,
)
from domains.runs.models import Run
from domains.runs.schemas import (
    Effort,
    RunDTO,
    RunTrigger,
)
from domains.runs.services.turn_service import run_to_dto
from domains.runs.services.lifecycle import LifecycleError, trigger_run
from domains.run_policies.services.effort import ensure_policy_for_effort
from domains.pagination import Page, page_params, paginate
from domains.tenancy import repo_scope, require_repo_in_org
from domains.users.schemas import UserDTO
from infrastructure.audit import write_audit

router = APIRouter(prefix="/api/v1/findings", tags=["findings"])


class BulkDeleteFindingsRequest(BaseModel):
    uids: list[str] = Field(default_factory=list)


@router.get("", response_model=list[FindingDTO], operation_id="opensweep_list_findings")
async def list_findings(
    response: Response,
    repository_uid: str | None = Query(None),
    source_run_uid: str | None = Query(None),
    tag: str | None = Query(None),
    kind: str | None = Query(None),
    exclude_kind: str | None = Query(None),
    status: str | None = Query(None),
    severity: str | None = Query(None),
    size: str | None = Query(None),
    sort_by: str = Query("updated_at"),
    sort_dir: str = Query("desc"),
    page: Page = Depends(page_params),
    user: UserDTO = Depends(get_current_user),
):
    """List findings.

    `status` takes any stored status, or the pseudo-value "processed" for
    everything a human has already dealt with (anything not "open") — that is
    what backs the board's Processed filter. Omitting it returns all statuses.
    """
    if sort_by not in FINDING_SORT_FIELDS:
        raise HTTPException(
            status_code=422,
            detail=f"sort_by must be one of {sorted(FINDING_SORT_FIELDS)}",
        )
    if sort_dir not in FINDING_SORT_DIRS:
        raise HTTPException(status_code=422, detail="sort_dir must be 'asc' or 'desc'")
    if repository_uid is not None:
        await require_repo_in_org(repository_uid, user.org_uid)
    items = await FindingService().list(
        repository_uids=await repo_scope(repository_uid, user.org_uid),
        source_run_uid=source_run_uid,
        tag=tag,
        kind=kind,
        exclude_kind=exclude_kind,
        status=status,
        severity=severity,
        size=size,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    return paginate(items, page, response)


@router.get(
    "/find-similar",
    response_model=list[FindingDTO],
    operation_id="opensweep_find_similar_finding",
)
async def find_similar_route(
    repository_uid: str = Query(...),
    dedupe_key: str | None = Query(None),
    title_substring: str | None = Query(None),
    user: UserDTO = Depends(get_current_user),
):
    await require_repo_in_org(repository_uid, user.org_uid)
    nodes = await find_similar(
        repository_uid=repository_uid,
        dedupe_key=dedupe_key,
        title_substring=title_substring,
    )
    return [finding_to_dto(n) for n in nodes]


@router.get("/{uid}", response_model=FindingDTO, operation_id="opensweep_get_finding")
async def get_finding(uid: str, user: UserDTO = Depends(get_current_user)):
    dto = await FindingService().get(uid)
    await require_repo_in_org(dto.repository_uid, user.org_uid)
    return dto


@router.post("", response_model=FindingDTO, operation_id="opensweep_file_finding")
async def file_finding(
    req: FileFindingRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    await require_repo_in_org(req.repository_uid, user.org_uid)
    return await FindingService().file_finding(req, actor_uid=user.uid)


@router.patch("/{uid}", response_model=FindingDTO, operation_id="opensweep_update_finding")
async def update_finding(
    uid: str,
    req: UpdateFindingRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    service = FindingService()
    f = await service.get_node(uid)
    await require_repo_in_org(f.repository_uid, user.org_uid)
    return await service.update(uid, req, actor_uid=user.uid)


@router.post(
    "/{uid}/dismiss", response_model=FindingDTO, operation_id="opensweep_dismiss_finding"
)
async def dismiss_finding(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    f = await FindingService().get_node(uid)
    await require_repo_in_org(f.repository_uid, user.org_uid)
    return await FindingService().dismiss(uid, actor_uid=user.uid)


@router.post(
    "/{uid}/acknowledge",
    response_model=FindingDTO,
    operation_id="opensweep_acknowledge_finding",
)
async def acknowledge_finding(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    f = await FindingService().get_node(uid)
    await require_repo_in_org(f.repository_uid, user.org_uid)
    return await FindingService().acknowledge(uid, actor_uid=user.uid)


@router.post(
    "/{uid}/wont-fix", response_model=FindingDTO, operation_id="opensweep_wont_fix_finding"
)
async def wont_fix_finding(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    f = await FindingService().get_node(uid)
    await require_repo_in_org(f.repository_uid, user.org_uid)
    return await FindingService().wont_fix(uid, actor_uid=user.uid)


@router.post(
    "/{uid}/mark-fixed", response_model=FindingDTO, operation_id="opensweep_mark_fixed_finding"
)
async def mark_fixed_finding(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    f = await FindingService().get_node(uid)
    await require_repo_in_org(f.repository_uid, user.org_uid)
    return await FindingService().mark_fixed(uid, actor_uid=user.uid)


@router.post("/bulk-delete", operation_id="opensweep_bulk_delete_findings")
async def bulk_delete_findings(
    req: BulkDeleteFindingsRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    service = FindingService()
    for uid in req.uids:
        f = await service.get_node(uid)
        await require_repo_in_org(f.repository_uid, user.org_uid)
    return await service.delete_many(req.uids, actor_uid=user.uid)


@router.delete("/{uid}", status_code=204, operation_id="opensweep_delete_finding")
async def delete_finding(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    f = await FindingService().get_node(uid)
    await require_repo_in_org(f.repository_uid, user.org_uid)
    await FindingService().delete(uid, actor_uid=user.uid)


def _build_verification_intent(f) -> str:
    paths = "\n".join(f"- {p}" for p in (f.affected_paths or [])) or "- (none reported)"
    return (
        "Verify whether the Finding below has been properly resolved in the current code.\n"
        "\n"
        f"Finding title: {f.title}\n"
        f"Tags: {', '.join(f.tags or []) or '(none)'}\n"
        f"Kind: {f.kind}\n"
        f"Severity: {f.severity}\n"
        f"Subtype: {f.subtype or '(none)'}\n"
        "\n"
        "Why it matters:\n"
        f"{(f.why_it_matters or '(not provided)').strip()}\n"
        "\n"
        "Suggested fix:\n"
        f"{(f.suggested_fix or '(not provided)').strip()}\n"
        "\n"
        "Affected paths:\n"
        f"{paths}\n"
        "\n"
        "Task:\n"
        "1. Read the affected paths and any closely-related code.\n"
        "2. Determine whether the originally reported problem is still present, partially "
        "addressed, fully resolved, or cannot be determined from the current code.\n"
        "3. Quote concrete file:line evidence for every claim you make.\n"
        "4. Conclude with a single line of the form `verdict: <resolved-properly|partially-resolved|"
        "not-resolved|cannot-determine>` followed by a paragraph explaining your reasoning.\n"
        "Do not file new Findings unless you find a *new* defect outside the scope of the "
        "original Finding."
    )


def _false_positive_instruction(policy: str) -> str:
    """What a refine run should do when it judges a Finding to be a false
    positive, per the owning org's `refine_false_positive_policy`."""
    if policy == "dismiss":
        return (
            "If it is NOT a real issue: call `opensweep_platform_update_finding` with "
            "`status: \"dismissed\"` and a reduced `confidence`, explaining in "
            "`description` why the reported problem does not actually hold."
        )
    if policy == "wont-fix":
        return (
            "If it is NOT a real issue: call `opensweep_platform_update_finding` with "
            "`status: \"wont-fix\"`, explaining in `description` why this will not "
            "be acted on."
        )
    return (
        "If it is NOT a real issue: do NOT change the status. Call "
        "`opensweep_platform_update_finding` to lower `confidence` and record your "
        "doubt in `description`/`root_cause`, leaving the finding `open` for a "
        "human to decide."
    )


def _build_finding_refine_intent(f, false_positive_policy: str) -> str:
    paths = "\n".join(f"- {p}" for p in (f.affected_paths or [])) or "- (none reported)"
    return (
        "Refine the Finding below: verify it is real, then sharpen it in place "
        "using the platform tools. This is read-only against the repository — "
        "do not modify any code.\n"
        "\n"
        f"Finding uid: {f.uid}\n"
        f"Finding title: {f.title}\n"
        f"Tags: {', '.join(f.tags or []) or '(none)'}\n"
        f"Kind: {f.kind}\n"
        f"Severity: {f.severity}\n"
        f"Confidence: {f.confidence}\n"
        "\n"
        "Current description:\n"
        f"{(f.description or '(not provided)').strip()}\n"
        "\n"
        "Affected paths:\n"
        f"{paths}\n"
        "\n"
        "Task:\n"
        "1. Read the affected paths and closely-related code to judge whether "
        "the reported problem is a genuine issue. Quote concrete file:line "
        "evidence.\n"
        "2. If it IS real, call `opensweep_platform_update_finding` (finding_uid "
        f"`{f.uid}`) to improve `title`, `description`, `root_cause`, "
        "`why_it_matters`, `suggested_fix`, and set `affected_paths` to the "
        "precise files involved.\n"
        "3. Attach a short remediation plan with `opensweep_platform_attach_artifact` "
        f"(target_type `finding`, target_uid `{f.uid}`, artifact_type `plan`) — "
        "the concrete steps a fixer should take, referencing the relevant files.\n"
        f"4. {_false_positive_instruction(false_positive_policy)}\n"
        "Persist every conclusion through the tools above — a description in your "
        "reply that is not written back does not count. Do not file new Findings "
        "unless you discover a *new* defect outside this Finding's scope."
    )


@router.post(
    "/{uid}/refine",
    response_model=RunDTO,
    operation_id="opensweep_refine_finding",
)
async def refine_finding(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    from domains.agents.services.composition import compose_agent_intent
    from domains.organizations.services.settings import get_settings_for_repo
    from domains.repositories.services.workflow import stage_prompt_body

    f = await FindingService().get_node(uid)
    await require_repo_in_org(f.repository_uid, user.org_uid)
    settings = await get_settings_for_repo(f.repository_uid)
    guidance = await stage_prompt_body(f.repository_uid, "refine")
    composed = await compose_agent_intent(
        repository_uid=f.repository_uid,
        agent_key="refine",
        stage="refine",
        repo_guidance=guidance or "",
        structural=_build_finding_refine_intent(f, settings.refine_false_positive_policy),
        org_uid=user.org_uid,
    )
    intent = composed.text
    policy = await ensure_policy_for_effort(Effort.NORMAL)

    await write_audit(
        kind="finding.refine.requested",
        subject_uid=uid,
        subject_type="Finding",
        actor_uid=user.uid,
        payload={"false_positive_policy": settings.refine_false_positive_policy},
    )

    try:
        run = await trigger_run(
            repository_uid=f.repository_uid,
            intent=intent,
            playbook="refine",
            title=f"Refine: {(f.title or 'finding')[:80]}",
            target={
                "finding_uid": uid,
                "affected_paths": list(f.affected_paths or []),
                "paths": list(f.affected_paths or []),
            },
            linked_finding_uid=uid,
            run_policy_uid=policy.uid,
            trigger=RunTrigger.MANUAL,
            triggered_by=user.uid,
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return run_to_dto(run)


@router.post(
    "/{uid}/verify",
    response_model=RunDTO,
    operation_id="opensweep_verify_finding",
)
async def verify_finding(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    from domains.agents.services.composition import compose_agent_intent
    from domains.repositories.services.workflow import stage_prompt_body

    f = await FindingService().get_node(uid)
    await require_repo_in_org(f.repository_uid, user.org_uid)
    guidance = await stage_prompt_body(f.repository_uid, "verify")
    composed = await compose_agent_intent(
        repository_uid=f.repository_uid,
        agent_key="verify",
        stage="verify",
        repo_guidance=guidance or "",
        structural=_build_verification_intent(f),
        org_uid=user.org_uid,
    )
    intent = composed.text
    policy = await ensure_policy_for_effort(Effort.NORMAL)

    await write_audit(
        kind="finding.verify.requested",
        subject_uid=uid,
        subject_type="Finding",
        actor_uid=user.uid,
        payload={},
    )

    try:
        run = await trigger_run(
            repository_uid=f.repository_uid,
            intent=intent,
            playbook="verify",
            title=f"Verify: {(f.title or 'finding')[:80]}",
            target={
                "finding_uid": uid,
                "affected_paths": list(f.affected_paths or []),
                "paths": list(f.affected_paths or []),
            },
            linked_finding_uid=uid,
            run_policy_uid=policy.uid,
            trigger=RunTrigger.MANUAL,
            triggered_by=user.uid,
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return run_to_dto(run)


@router.get(
    "/{uid}/verifications",
    response_model=list[RunDTO],
    operation_id="opensweep_list_finding_verifications",
)
async def list_finding_verifications(uid: str, user: UserDTO = Depends(get_current_user)):
    f = await FindingService().get_node(uid)
    await require_repo_in_org(f.repository_uid, user.org_uid)
    matching = list(await Run.nodes.filter(linked_finding_uid=uid, playbook="verify"))
    matching.sort(
        key=lambda r: r.started_at
        or r.created_at
        or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return [run_to_dto(r) for r in matching]
