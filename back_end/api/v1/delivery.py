"""Delivery routes — PR convergence ledger, finding triage actions, verdicts.

The triage actions are the six UI verbs from PLATFORM_V2_DESIGN.md §4:
fix-now happens via attach-fix (fixer claims), verify (review grants),
waive, defer (→ ticket), blocking-override (not-important / escalate).
"""

from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from api.dependencies import get_current_user, require_role
from domains.tenancy import org_repo_uids, require_repo_in_org
from domains.delivery.schemas import (
    AttachFixRequest,
    BlockingOverrideRequest,
    ConvergenceState,
    FindingResolutionDTO,
    MergePolicyDTO,
    PullRequestDTO,
    SubmitVerdictRequest,
    UpdateMergePolicyRequest,
    VerdictDTO,
    WaiveRequest,
)
from domains.delivery.services.pull_request_service import (
    PullRequestService,
    latest_verdict_for,
    pull_request_to_dto,
    verdict_to_dto,
)
from domains.delivery.services.resolution_service import (
    ResolutionService,
    ensure_merge_policy,
    merge_policy_to_dto,
    resolution_to_dto,
)
from domains.findings.models import Finding
from domains.investigations.schemas import InvestigationEffort, normalize_effort
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1/delivery", tags=["delivery"])


# ── Pull requests ────────────────────────────────────────────────────────────


@router.get("/pull-requests", response_model=list[PullRequestDTO], operation_id="opensweep_list_pull_requests")
async def list_pull_requests(
    repository_uid: str | None = Query(None),
    state: str | None = Query(None),
    user: UserDTO = Depends(get_current_user),
):
    if repository_uid is not None:
        await require_repo_in_org(repository_uid, user.org_uid)
    prs = await PullRequestService().list(repository_uid=repository_uid, state=state)
    if repository_uid is None:
        allowed = await org_repo_uids(user.org_uid)
        prs = [pr for pr in prs if pr.repository_uid in allowed]
    await _attach_waive_requested_counts(prs)
    return prs


async def _attach_waive_requested_counts(prs: list[PullRequestDTO]) -> None:
    """Populate waive_requested_count for a batch of PR DTOs in one query —
    a pending waiver is a resolution with a request reason not yet approved
    (state != 'waived'), the signal the queue's 'needs you' column reads."""
    if not prs:
        return
    from neomodel import adb

    uids = [pr.uid for pr in prs]
    rows, _ = await adb.cypher_query(
        "MATCH (r:FindingResolution) "
        "WHERE r.pull_request_uid IN $uids AND r.waive_requested_reason <> '' "
        "AND r.state <> 'waived' "
        "RETURN r.pull_request_uid, count(r)",
        {"uids": uids},
    )
    counts = {row[0]: int(row[1]) for row in rows}
    for pr in prs:
        pr.waive_requested_count = counts.get(pr.uid, 0)


@router.get("/pull-requests/{uid}", response_model=PullRequestDTO, operation_id="opensweep_get_pull_request")
async def get_pull_request(uid: str, user: UserDTO = Depends(get_current_user)):
    pr = await PullRequestService().get_node(uid)
    await require_repo_in_org(pr.repository_uid, user.org_uid)
    return pull_request_to_dto(pr)


class PullRequestFilesDTO(BaseModel):
    """Changed files + per-file unified diffs of a PR (Files panel). Same
    shape as `RunChangesDTO` so the frontend diff panel is shared; `tree`
    stays empty (no workspace behind a synced PR)."""

    source: str = "none"
    base: str = ""
    captured_at: str | None = None
    files: list[dict] = []
    tree: list[str] = []


@router.get(
    "/pull-requests/{uid}/files",
    response_model=PullRequestFilesDTO,
    operation_id="opensweep_pull_request_files",
)
async def get_pull_request_files(uid: str, user: UserDTO = Depends(get_current_user)):
    """The PR's diff straight from the provider (GET /pulls/{n}/files)."""
    service = PullRequestService()
    pr = await service.get_node(uid)
    await require_repo_in_org(pr.repository_uid, user.org_uid)
    return PullRequestFilesDTO(**await service.files(pr))


@router.post(
    "/pull-requests/sync",
    response_model=PullRequestDTO,
    operation_id="opensweep_sync_pull_request",
)
async def sync_pull_request(
    repository_uid: str = Query(...),
    github_number: int = Query(...),
    user: UserDTO = Depends(require_role("maintainer")),
):
    """Manual head-driven resync — same path the webhook takes."""
    await require_repo_in_org(repository_uid, user.org_uid)
    return await PullRequestService().sync_from_github(repository_uid, github_number)


@router.post(
    "/pull-requests/sync-repo",
    operation_id="opensweep_sync_repository_pull_requests",
)
async def sync_repository_pull_requests(
    repository_uid: str = Query(...),
    user: UserDTO = Depends(require_role("maintainer")),
) -> dict:
    """Full 2-way reconcile: import every open GitHub PR (including ones
    opened outside OpenSweep) and close out PRs merged/closed externally."""
    await require_repo_in_org(repository_uid, user.org_uid)
    return await PullRequestService().sync_repository(repository_uid)


@router.post(
    "/pull-requests/{uid}/recompute",
    response_model=ConvergenceState,
    operation_id="opensweep_recompute_convergence",
)
async def recompute_convergence(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    service = PullRequestService()
    pr = await service.get_node(uid)
    await require_repo_in_org(pr.repository_uid, user.org_uid)
    return await service.recompute_and_publish(pr)


class TriggerReviewRequest(BaseModel):
    # Recall/precision dial: quick = top-5 blocking-only, deep = exhaustive
    # multi-lens. Applies to this run only; auto reviews use the workflow config.
    depth: InvestigationEffort = InvestigationEffort.NORMAL
    # Force a full base...head review even when a prior verdict would allow
    # an incremental one.
    full: bool = False
    # Numeric budget: caps normal/deep and overrides quick's default of 5.
    max_findings: int | None = Field(default=None, ge=1, le=50)

    @field_validator("depth", mode="before")
    @classmethod
    def _normalize_depth(cls, v):
        if v is None:
            return v
        return normalize_effort(v if isinstance(v, str) else (v.value if v else ""))


@router.post(
    "/pull-requests/{uid}/review",
    operation_id="opensweep_trigger_review_run",
)
async def trigger_review(
    uid: str,
    req: TriggerReviewRequest | None = None,
    user: UserDTO = Depends(require_role("maintainer")),
) -> dict:
    """Dispatch a read-only review run that ends with a SHA-bound verdict (§6)."""
    from domains.delivery.services.review_run_service import trigger_review_run
    from domains.investigations.services.lifecycle import LifecycleError

    pr = await PullRequestService().get_node(uid)
    await require_repo_in_org(pr.repository_uid, user.org_uid)
    if pr.state != "open":
        raise HTTPException(status_code=409, detail=f"PR is {pr.state}; only open PRs are reviewed")
    if not pr.head_sha:
        raise HTTPException(status_code=409, detail="PR has no head sha — sync it first")
    try:
        run = await trigger_review_run(
            pr,
            triggered_by=user.uid,
            depth=req.depth if req else InvestigationEffort.NORMAL,
            force_full=bool(req.full) if req else False,
            max_findings=req.max_findings if req else None,
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "run_uid": run.uid,
        "investigation_uid": run.investigation_uid,
        "head_sha": pr.head_sha,
        "depth": (run.target or {}).get("depth", ""),
        "incremental_from": (run.target or {}).get("prior_verdict_sha", ""),
    }


class TriggerFixRequest(BaseModel):
    finding_uids: list[str] = Field(default_factory=list)


@router.post(
    "/pull-requests/{uid}/fix",
    operation_id="opensweep_trigger_fix_run",
)
async def trigger_fix(
    uid: str,
    req: TriggerFixRequest | None = None,
    user: UserDTO = Depends(require_role("maintainer")),
) -> dict:
    """Dispatch a write-path fix run on the PR's head branch (§6).

    Bounded by MergePolicy.max_fix_rounds (409 when exhausted — human
    required). The agent commits in a write sandbox; the platform gate
    validates and pushes to the SAME branch."""
    from domains.delivery.services.fix_run_service import trigger_fix_run
    from domains.investigations.services.lifecycle import LifecycleError

    pr = await PullRequestService().get_node(uid)
    await require_repo_in_org(pr.repository_uid, user.org_uid)
    try:
        run = await trigger_fix_run(
            pr,
            triggered_by=user.uid,
            finding_uids=(req.finding_uids if req else None) or None,
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "run_uid": run.uid,
        "investigation_uid": run.investigation_uid,
        "fix_round": int(pr.fix_rounds or 0),
        "head_ref": pr.head_ref,
    }


@router.post(
    "/pull-requests/{uid}/reset-fix-rounds",
    response_model=PullRequestDTO,
    operation_id="opensweep_reset_fix_rounds",
)
async def reset_fix_rounds(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    """Reset the bounded auto-fix loop counter (§6) so a maintainer can grant
    another round of fix runs after 'human required'. Audited; recomputes the
    stored fix_rounds_exhausted flag and convergence snapshot."""
    from datetime import datetime

    from infrastructure.audit import write_audit

    service = PullRequestService()
    pr = await service.get_node(uid)
    await require_repo_in_org(pr.repository_uid, user.org_uid)
    previous = int(pr.fix_rounds or 0)
    pr.fix_rounds = 0
    pr.updated_at = datetime.now(UTC)
    await pr.save()
    await write_audit(
        kind="pull_request.fix_rounds_reset",
        subject_uid=pr.uid,
        subject_type="PullRequest",
        actor_uid=user.uid,
        payload={"previous_fix_rounds": previous},
    )
    # Refresh the denormalized exhausted flag + convergence snapshot.
    await service.recompute_and_publish(pr)
    return pull_request_to_dto(pr)


class LinkTicketRequest(BaseModel):
    ticket_uid: str = Field(min_length=1)


@router.post(
    "/pull-requests/{uid}/link-ticket",
    response_model=PullRequestDTO,
    operation_id="opensweep_link_pr_to_ticket",
)
async def link_pr_to_ticket(
    uid: str, req: LinkTicketRequest, user: UserDTO = Depends(require_role("maintainer"))
):
    """Bind a PR to the ticket it implements. The ticket auto-advances to
    in-review when it was todo/in-progress (system actor, audited)."""
    from datetime import datetime

    from domains.tickets.services.ticket_service import TicketService
    from infrastructure.audit import write_audit

    service = PullRequestService()
    pr = await service.get_node(uid)
    await require_repo_in_org(pr.repository_uid, user.org_uid)
    ticket_service = TicketService()
    ticket = await ticket_service.get_node(req.ticket_uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    if ticket.repository_uid != pr.repository_uid:
        raise HTTPException(status_code=409, detail="cross-repository link not allowed")

    pr.ticket_uid = ticket.uid
    pr.updated_at = datetime.now(UTC)
    await pr.save()
    await write_audit(
        kind="pull_request.ticket_linked",
        subject_uid=pr.uid,
        subject_type="PullRequest",
        actor_uid=user.uid,
        payload={"ticket_uid": ticket.uid},
    )
    await ticket_service.link_pr(ticket.uid, pr.uid, actor_uid=user.uid)
    return pull_request_to_dto(pr)


class CreateTicketForPrRequest(BaseModel):
    """mode `manual`: placeholder ticket from PR metadata, human fills it in.
    mode `ai`: same + a refine run drafts the content from the PR's diff."""

    mode: str = Field(default="manual", pattern="^(manual|ai)$")


@router.post(
    "/pull-requests/{uid}/create-ticket",
    operation_id="opensweep_pr_create_ticket",
)
async def create_ticket_for_pr(
    uid: str,
    req: CreateTicketForPrRequest,
    user: UserDTO = Depends(require_role("maintainer")),
) -> dict:
    """Adopt an externally-opened PR into the board: create its ticket, link
    both directions, and (mode `ai`) draft the ticket content from the PR's
    actual diff via a read-only refine run.

    The new ticket is born under review — the work already exists as a PR,
    and the human clicking create IS the Gate-1 approval (audited)."""
    from datetime import datetime

    from domains.tickets.schemas import CreateTicketRequest
    from domains.tickets.services.refine_dispatch import dispatch_refine_run
    from domains.tickets.services.ticket_service import TicketService
    from infrastructure.audit import write_audit

    service = PullRequestService()
    pr = await service.get_node(uid)
    await require_repo_in_org(pr.repository_uid, user.org_uid)
    if pr.ticket_uid:
        raise HTTPException(status_code=409, detail="PR already has a ticket")

    branch_line = f"`{pr.head_ref}` → `{pr.base_ref}`"
    description = (
        f"Imported from pull request [#{pr.github_number}]({pr.url}) ({branch_line}), "
        "which was opened outside OpenSweep.\n\n"
        "_Describe what the change does and why — or let the refine agent draft "
        "it from the PR's diff._"
    )
    ticket_service = TicketService()
    ticket = await ticket_service.create(
        CreateTicketRequest(
            repository_uid=pr.repository_uid,
            title=pr.title or f"PR #{pr.github_number}",
            description=description,
            labels=["pr-import"],
        ),
        actor_uid=user.uid,
    )
    # Board placement matches reality: an open PR is work under review, a
    # merged one is done. Direct set (not transition()) — this is an import,
    # not a board move; the explicit human create carries Gate 1.
    now = datetime.now(UTC)
    ticket.approved_by = user.uid
    ticket.approved_at = now
    target_status = {"open": "in-review", "merged": "done"}.get(pr.state or "open")
    if target_status:
        await ticket_service._set_status(ticket, target_status)  # noqa: SLF001
    else:
        await ticket.save()
    await write_audit(
        kind="ticket.imported_from_pr",
        subject_uid=ticket.uid,
        subject_type="Ticket",
        actor_uid=user.uid,
        payload={"pull_request_uid": pr.uid, "status": ticket.status, "mode": req.mode},
    )

    pr.ticket_uid = ticket.uid
    pr.updated_at = now
    await pr.save()
    await ticket_service.link_pr(ticket.uid, pr.uid, actor_uid=user.uid, auto_review=False)

    run_uid = ""
    if req.mode == "ai":
        extra = (
            "IMPORTANT CONTEXT — this ticket is a placeholder imported from an "
            f"EXISTING pull request #{pr.github_number} (head `{pr.head_ref}`, "
            f"base `{pr.base_ref}`). Ground your refinement in the PR's actual "
            f"changes: `git fetch origin {pr.head_ref}`, then read "
            f"`git diff {pr.base_ref}...origin/{pr.head_ref}` (read-only). "
            "Write the title, description and acceptance criteria so they "
            "describe what the PR changes and why — good enough for a reviewer "
            "to judge the PR against."
        )
        run = await dispatch_refine_run(
            ticket, actor_uid=user.uid, org_uid=user.org_uid, extra_context=extra
        )
        run_uid = run.uid

    return {"ticket_uid": ticket.uid, "run_uid": run_uid}


@router.get(
    "/pull-requests/{uid}/verdict",
    response_model=VerdictDTO | None,
    operation_id="opensweep_get_latest_verdict",
)
async def get_latest_verdict(uid: str, user: UserDTO = Depends(get_current_user)):
    pr = await PullRequestService().get_node(uid)
    await require_repo_in_org(pr.repository_uid, user.org_uid)
    v = await latest_verdict_for(uid)
    return verdict_to_dto(v) if v else None


@router.post(
    "/pull-requests/{uid}/verdicts",
    response_model=VerdictDTO,
    operation_id="opensweep_submit_verdict",
)
async def submit_verdict(
    uid: str, req: SubmitVerdictRequest, user: UserDTO = Depends(require_role("maintainer"))
):
    service = PullRequestService()
    pr = await service.get_node(uid)
    await require_repo_in_org(pr.repository_uid, user.org_uid)
    return await service.submit_verdict(uid, req, actor_uid=user.uid)


# ── Resolutions (the per-PR findings ledger) ─────────────────────────────────


@router.get(
    "/pull-requests/{uid}/resolutions",
    response_model=list[FindingResolutionDTO],
    operation_id="opensweep_list_pr_resolutions",
)
async def list_pr_resolutions(uid: str, user: UserDTO = Depends(get_current_user)):
    pr = await PullRequestService().get_node(uid)
    await require_repo_in_org(pr.repository_uid, user.org_uid)
    return await ResolutionService().list_for_pr(uid)


@router.post(
    "/pull-requests/{uid}/resolutions/{finding_uid}",
    response_model=FindingResolutionDTO,
    operation_id="opensweep_bind_finding_to_pr",
)
async def bind_finding_to_pr(
    uid: str, finding_uid: str, user: UserDTO = Depends(require_role("maintainer"))
):
    pr = await PullRequestService().get_node(uid)
    await require_repo_in_org(pr.repository_uid, user.org_uid)
    finding = await Finding.nodes.get_or_none(uid=finding_uid)
    if finding is None:
        raise HTTPException(status_code=404, detail="not found")
    await require_repo_in_org(finding.repository_uid, user.org_uid)
    if finding.repository_uid != pr.repository_uid:
        raise HTTPException(status_code=409, detail="cross-repository link not allowed")
    service = ResolutionService()
    r = await service.ensure(finding_uid=finding_uid, pull_request_uid=uid)
    return await _resolution_dto(r)


async def _resolution_dto(r) -> FindingResolutionDTO:
    policy = await ensure_merge_policy(r.repository_uid)
    finding = await Finding.nodes.get_or_none(uid=r.finding_uid)
    return resolution_to_dto(r, finding, dict(policy.blocking or {}))


async def _after_transition(r) -> FindingResolutionDTO:
    """Every ledger transition recomputes + republishes convergence."""
    service = PullRequestService()
    pr = await service.get_node(r.pull_request_uid)
    await service.recompute_and_publish(pr)
    return await _resolution_dto(r)


@router.post(
    "/resolutions/{uid}/attach-fix",
    response_model=FindingResolutionDTO,
    operation_id="opensweep_attach_fix",
)
async def attach_fix(uid: str, req: AttachFixRequest, user: UserDTO = Depends(require_role("maintainer"))):
    service = ResolutionService()
    resolution = await service.get_node(uid)
    await require_repo_in_org(resolution.repository_uid, user.org_uid)
    r = await service.attach_fix(uid, sha=req.sha, actor_uid=user.uid)
    return await _after_transition(r)


@router.post(
    "/resolutions/{uid}/verify",
    response_model=FindingResolutionDTO,
    operation_id="opensweep_verify_resolution",
)
async def verify_resolution(
    uid: str,
    req: AttachFixRequest,
    run_uid: str = Query(""),
    user: UserDTO = Depends(require_role("maintainer")),
):
    """Human REST variant — review-runs verify through the tool surface."""
    service = ResolutionService()
    resolution = await service.get_node(uid)
    await require_repo_in_org(resolution.repository_uid, user.org_uid)
    r = await service.verify(uid, sha=req.sha, run_uid=run_uid, actor_uid=user.uid)
    return await _after_transition(r)


@router.post(
    "/resolutions/{uid}/waive",
    response_model=FindingResolutionDTO,
    operation_id="opensweep_waive_resolution",
)
async def waive_resolution(
    uid: str, req: WaiveRequest, user: UserDTO = Depends(require_role("maintainer"))
):
    service = ResolutionService()
    resolution = await service.get_node(uid)
    await require_repo_in_org(resolution.repository_uid, user.org_uid)
    r = await service.waive(uid, reason=req.reason, actor_uid=user.uid)
    return await _after_transition(r)


@router.post(
    "/resolutions/{uid}/defer",
    response_model=FindingResolutionDTO,
    operation_id="opensweep_defer_resolution",
)
async def defer_resolution(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    service = ResolutionService()
    resolution = await service.get_node(uid)
    await require_repo_in_org(resolution.repository_uid, user.org_uid)
    r, _ticket = await service.defer(uid, actor_uid=user.uid)
    return await _after_transition(r)


@router.post(
    "/resolutions/{uid}/reopen",
    response_model=FindingResolutionDTO,
    operation_id="opensweep_reopen_resolution",
)
async def reopen_resolution(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    service = ResolutionService()
    resolution = await service.get_node(uid)
    await require_repo_in_org(resolution.repository_uid, user.org_uid)
    r = await service.reopen(uid, actor_uid=user.uid)
    return await _after_transition(r)


@router.post(
    "/resolutions/{uid}/blocking-override",
    response_model=FindingResolutionDTO,
    operation_id="opensweep_set_blocking_override",
)
async def set_blocking_override(
    uid: str, req: BlockingOverrideRequest, user: UserDTO = Depends(require_role("maintainer"))
):
    service = ResolutionService()
    resolution = await service.get_node(uid)
    await require_repo_in_org(resolution.repository_uid, user.org_uid)
    r = await service.set_blocking_override(
        uid, override=req.override.value, reason=req.reason, actor_uid=user.uid
    )
    return await _after_transition(r)


# ── Merge policy ─────────────────────────────────────────────────────────────


@router.get(
    "/repositories/{repository_uid}/merge-policy",
    response_model=MergePolicyDTO,
    operation_id="opensweep_get_merge_policy",
)
async def get_merge_policy(repository_uid: str, user: UserDTO = Depends(get_current_user)):
    await require_repo_in_org(repository_uid, user.org_uid)
    return merge_policy_to_dto(await ensure_merge_policy(repository_uid))


@router.put(
    "/repositories/{repository_uid}/merge-policy",
    response_model=MergePolicyDTO,
    operation_id="opensweep_update_merge_policy",
)
async def update_merge_policy(
    repository_uid: str,
    req: UpdateMergePolicyRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    import re as _re
    from datetime import datetime

    from infrastructure.audit import write_audit

    await require_repo_in_org(repository_uid, user.org_uid)
    policy = await ensure_merge_policy(repository_uid)
    if req.blocking is not None:
        policy.blocking = req.blocking
    if req.require_clean_round is not None:
        policy.require_clean_round = req.require_clean_round
    if req.max_fix_rounds is not None:
        policy.max_fix_rounds = req.max_fix_rounds
    if req.path_denylist is not None:
        for pattern in req.path_denylist:
            try:
                _re.compile(pattern)
            except _re.error as exc:
                raise HTTPException(
                    status_code=422, detail=f"invalid denylist regex {pattern!r}: {exc}"
                ) from exc
        policy.path_denylist = req.path_denylist
    policy.updated_at = datetime.now(UTC)
    await policy.save()
    await write_audit(
        kind="merge_policy.updated",
        subject_uid=policy.uid,
        subject_type="MergePolicy",
        actor_uid=user.uid,
        payload=req.model_dump(exclude_none=True),
    )
    # The policy is a convergence input: refresh every OPEN PR of this repo
    # so stored predicates/status checks don't go stale (best-effort).
    from domains.delivery.services.pull_request_service import (
        recompute_open_prs_for_repository,
    )

    await recompute_open_prs_for_repository(repository_uid)
    return merge_policy_to_dto(policy)
