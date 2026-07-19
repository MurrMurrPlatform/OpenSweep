"""Thread routes — the unified dev flow's conversation-per-ticket surface.

Conversation I/O happens on the existing /runs endpoints (messages /
interrupt / ws) against the thread's active_run_uid; these routes own
lifecycle: create, plan gate, implement, abandon.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from api.dependencies import get_current_user, require_role
from domains.tenancy import require_repo_in_org
from domains.threads.schemas import (
    CreateThreadRequest,
    ThreadDetailDTO,
    ThreadDTO,
    UpdateThreadPlanRequest,
)
from domains.threads.services.thread_service import ThreadService, thread_to_dto
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1/threads", tags=["threads"])


@router.get("", response_model=list[ThreadDTO], operation_id="opensweep_thread_list")
async def list_threads(
    repository_uid: str | None = Query(None),
    subject_ticket_uid: str | None = Query(None),
    user: UserDTO = Depends(get_current_user),
):
    if repository_uid is not None:
        await require_repo_in_org(repository_uid, user.org_uid)
    threads = await ThreadService().list(
        repository_uid=repository_uid or "",
        subject_ticket_uid=subject_ticket_uid or "",
    )
    if repository_uid is None:
        from domains.tenancy import org_repo_uids

        allowed = await org_repo_uids(user.org_uid)
        threads = [t for t in threads if t.repository_uid in allowed]
    return [thread_to_dto(t) for t in threads]


@router.post("", response_model=ThreadDTO, operation_id="opensweep_thread_create")
async def create_thread(
    req: CreateThreadRequest, user: UserDTO = Depends(require_role("maintainer"))
):
    from domains.tickets.services.ticket_service import TicketService

    ticket = await TicketService().get_node(req.ticket_uid)
    await require_repo_in_org(ticket.repository_uid, user.org_uid)
    t = await ThreadService().create(
        ticket_uid=req.ticket_uid, actor_uid=user.uid, org_uid=user.org_uid
    )
    return thread_to_dto(t)


@router.get("/{uid}", response_model=ThreadDetailDTO, operation_id="opensweep_thread_get")
async def get_thread(uid: str, user: UserDTO = Depends(get_current_user)):
    svc = ThreadService()
    t = await svc.get_node(uid)
    await require_repo_in_org(t.repository_uid, user.org_uid)
    return await svc.get_detail(uid)


@router.patch(
    "/{uid}/plan", response_model=ThreadDTO, operation_id="opensweep_thread_update_plan"
)
async def update_plan(
    uid: str,
    req: UpdateThreadPlanRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    svc = ThreadService()
    t = await svc.get_node(uid)
    await require_repo_in_org(t.repository_uid, user.org_uid)
    t = await svc.update_plan(uid, req.plan_text, actor_uid=user.uid)
    return thread_to_dto(t)


@router.post(
    "/{uid}/plan/approve",
    response_model=ThreadDTO,
    operation_id="opensweep_thread_approve_plan",
)
async def approve_plan(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    svc = ThreadService()
    t = await svc.get_node(uid)
    await require_repo_in_org(t.repository_uid, user.org_uid)
    t = await svc.approve_plan(uid, actor_uid=user.uid)
    return thread_to_dto(t)


@router.post("/{uid}/implement", operation_id="opensweep_thread_implement")
async def implement_thread(
    uid: str, user: UserDTO = Depends(require_role("maintainer"))
) -> dict:
    svc = ThreadService()
    t = await svc.get_node(uid)
    await require_repo_in_org(t.repository_uid, user.org_uid)
    run = await svc.start_implement(uid, actor_uid=user.uid)
    return {"run_uid": run.uid, "thread_uid": uid}


class AnswerQuestionRequest(BaseModel):
    answer: str = Field(min_length=1)


@router.post(
    "/{uid}/questions/{question_uid}/answer",
    response_model=ThreadDetailDTO,
    operation_id="opensweep_thread_answer_question",
)
async def answer_question(
    uid: str,
    question_uid: str,
    req: AnswerQuestionRequest,
    user: UserDTO = Depends(require_role("maintainer")),
):
    svc = ThreadService()
    t = await svc.get_node(uid)
    await require_repo_in_org(t.repository_uid, user.org_uid)
    await svc.answer_question(uid, question_uid, req.answer, actor_uid=user.uid)
    return await svc.get_detail(uid)


@router.post(
    "/{uid}/questions/continue",
    response_model=ThreadDetailDTO,
    operation_id="opensweep_thread_continue_questions",
)
async def continue_without_answers(
    uid: str, user: UserDTO = Depends(require_role("maintainer"))
):
    """Force the conversation on: delivers accumulated answers and dismisses
    the still-open questions ('the user chose to proceed')."""
    svc = ThreadService()
    t = await svc.get_node(uid)
    await require_repo_in_org(t.repository_uid, user.org_uid)
    await svc.continue_without_answers(uid, actor_uid=user.uid)
    return await svc.get_detail(uid)


@router.post(
    "/{uid}/request-review",
    response_model=ThreadDTO,
    operation_id="opensweep_thread_request_review",
)
async def request_review(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    """Human ready-for-review signal — same flag and same platform reaction
    as the agent's `submit_for_review` tool, applied immediately (no need to
    wait for a turn boundary): un-draft the PR, auto-dispatch the review when
    the repo's workflow has review on auto."""
    from domains.platform_tools.submit_for_review import submit_for_review
    from domains.threads.services.hooks import maybe_ready_and_review_for_thread

    svc = ThreadService()
    t = await svc.get_node(uid)
    await require_repo_in_org(t.repository_uid, user.org_uid)
    await submit_for_review(thread_uid=uid, executor=user.uid)
    await maybe_ready_and_review_for_thread(uid)
    return thread_to_dto(await svc.get_node(uid))


@router.post(
    "/{uid}/abandon", response_model=ThreadDTO, operation_id="opensweep_thread_abandon"
)
async def abandon_thread(uid: str, user: UserDTO = Depends(require_role("maintainer"))):
    svc = ThreadService()
    t = await svc.get_node(uid)
    await require_repo_in_org(t.repository_uid, user.org_uid)
    t = await svc.abandon(uid, actor_uid=user.uid)
    return thread_to_dto(t)
