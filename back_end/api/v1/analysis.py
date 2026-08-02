"""Analysis routes — the deep-scan report surface.

Read + interaction only; the report is AUTHORED by the agent through the
platform tools (upsert_analysis / set_analysis_section / add_analysis_note /
ask_question). Interactive question answering + refine-with-answers land in
Phase C.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from api.dependencies import get_current_user, require_role
from domains.analysis.schemas import AnalysisDTO
from domains.analysis.services.analysis_service import AnalysisService
from domains.pagination import Page, page_params, paginate
from domains.tenancy import repo_scope, require_repo_in_org
from domains.users.schemas import UserDTO
from infrastructure.kill_switch import KillSwitchActiveError, assert_runnable

router = APIRouter(prefix="/api/v1/analyses", tags=["analysis"])


class AnswerQuestionRequest(BaseModel):
    answer: str


class RefineResult(BaseModel):
    analysis_uid: str
    run_uid: str
    supersedes: str


@router.get("", response_model=list[AnalysisDTO], operation_id="opensweep_list_analyses")
async def list_analyses(
    response: Response,
    repository_uid: str | None = Query(None),
    status: str | None = Query(None),
    include_superseded: bool = Query(True),
    page: Page = Depends(page_params),
    user: UserDTO = Depends(get_current_user),
):
    if repository_uid is not None:
        await require_repo_in_org(repository_uid, user.org_uid)
    items = await AnalysisService().list(
        repository_uids=await repo_scope(repository_uid, user.org_uid),
        status=status,
        include_superseded=include_superseded,
    )
    return paginate(items, page, response)


# Declared before /{uid} so the static path wins the match. Latest
# non-superseded Analysis for a repo — the Health view's "current" report.
@router.get(
    "/latest",
    response_model=AnalysisDTO | None,
    operation_id="opensweep_latest_analysis",
)
async def latest_analysis(
    repository_uid: str = Query(...), user: UserDTO = Depends(get_current_user)
):
    await require_repo_in_org(repository_uid, user.org_uid)
    return await AnalysisService().latest_for_repo(repository_uid)


@router.get(
    "/{uid}", response_model=AnalysisDTO, operation_id="opensweep_get_analysis"
)
async def get_analysis(uid: str, user: UserDTO = Depends(get_current_user)):
    dto = await AnalysisService().get(uid)
    await require_repo_in_org(dto.repository_uid, user.org_uid)
    return dto


@router.post(
    "/{uid}/questions/{qid}/answer",
    response_model=AnalysisDTO,
    operation_id="opensweep_answer_analysis_question",
)
async def answer_question(
    uid: str,
    qid: str,
    req: AnswerQuestionRequest,
    user: UserDTO = Depends(get_current_user),
):
    svc = AnalysisService()
    node = await svc.get_node(uid)
    await require_repo_in_org(node.repository_uid, user.org_uid)
    return await svc.answer_question(uid, qid, answer=req.answer, actor=user.uid)


@router.post(
    "/{uid}/questions/{qid}/dismiss",
    response_model=AnalysisDTO,
    operation_id="opensweep_dismiss_analysis_question",
)
async def dismiss_question(
    uid: str, qid: str, user: UserDTO = Depends(get_current_user)
):
    svc = AnalysisService()
    node = await svc.get_node(uid)
    await require_repo_in_org(node.repository_uid, user.org_uid)
    return await svc.dismiss_question(uid, qid)


@router.post(
    "/{uid}/refine",
    response_model=RefineResult,
    operation_id="opensweep_refine_analysis",
)
async def refine_analysis(
    uid: str, user: UserDTO = Depends(require_role("maintainer"))
):
    """Dispatch a fresh deep-scan that ingests the answered questions; the new
    run's Analysis supersedes this one."""
    svc = AnalysisService()
    node = await svc.get_node(uid)
    await require_repo_in_org(node.repository_uid, user.org_uid)
    try:
        await assert_runnable(node.repository_uid)
    except KillSwitchActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await svc.refine_with_answers(uid, triggered_by=user.uid)
