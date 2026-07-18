"""HTTP transport for the comment platform tools.

Executors read a thread with `opensweep_platform_list_comments` (mounted with the
delivery tools) and reply with `opensweep_platform_add_comment` here. Replies are
attributed to the run: author_kind="opensweep", source_run_uid from the run-token
header, so the UI can render agent answers distinctly and maintainers can
trace every agent comment back to its run.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.dependencies import get_current_user
from api.platform_scope import require_tool_repo_access
from domains.comments import service as comment_service
from domains.comments.schemas import (
    CommentAuthorKind,
    CommentDTO,
    CommentSubjectType,
)
from domains.comments.subjects import get_subject
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1/platform-tools/comments", tags=["platform_tools"])


class AddCommentRequest(BaseModel):
    subject_type: CommentSubjectType
    subject_uid: str = Field(min_length=1)
    body: str = Field(min_length=1)


THREAD_COMMENT_REJECTION = (
    "add_comment is disabled inside thread conversations. Ask the user "
    "questions with `opensweep_platform_ask_user`, submit or update the plan "
    "with `opensweep_platform_submit_thread_plan`, and say everything else "
    "directly in this conversation — the platform mirrors your questions to "
    "the ticket's discussion for you."
)


def thread_comment_error(playbook: str) -> str | None:
    """Structural (not prompt-level) guard: thread-playbook runs may not post
    comments — dogfooding showed agents route plans/questions there no matter
    what the intent says. The rejection text teaches the model the correct
    tools mid-run."""
    return THREAD_COMMENT_REJECTION if (playbook or "") == "thread" else None


@router.post(
    "",
    response_model=CommentDTO,
    operation_id="opensweep_platform_add_comment",
)
async def platform_add_comment(
    req: AddCommentRequest,
    request: Request,
    user: UserDTO = Depends(get_current_user),
):
    """Reply on a data item's comment thread.

    This is how a run answers the human conversation — REQUIRED after an
    @opensweep summon, encouraged whenever a maintainer's comment deserves a
    status update. Keep replies concise and maintainer-facing."""
    subject = await get_subject(req.subject_type, req.subject_uid)
    if subject is None:
        raise HTTPException(status_code=404, detail="not found")
    await require_tool_repo_access(request, user, subject.repository_uid)
    run_uid = request.headers.get("X-OpenSweep-Run-Uid") or ""
    if run_uid:
        from domains.investigations.models import Run

        run = await Run.nodes.get_or_none(uid=run_uid)
        rejection = thread_comment_error(run.playbook if run else "")
        if rejection:
            raise HTTPException(status_code=403, detail=rejection)
    c = await comment_service.create_comment(
        subject_type=req.subject_type,
        subject_uid=req.subject_uid,
        body=req.body,
        author_uid=run_uid or user.uid,
        author_kind=CommentAuthorKind.OPENSWEEP if run_uid else CommentAuthorKind.USER,
        source_run_uid=run_uid,
    )
    return await comment_service.comment_to_dto(c)
