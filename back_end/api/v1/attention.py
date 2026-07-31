"""Repository attention rollup — the workspace Overview's "needs attention"
panel.

Current-state aggregation (see domains/metrics/services/attention_service.py):
kill switch, PRs blocked on a human, broken runs, pending approvals, and
backlog aggregates, in one prioritized list.
"""

from fastapi import APIRouter, Depends

from api.dependencies import get_current_user
from domains.metrics.schemas import RepoAttention
from domains.metrics.services.attention_service import attention_service
from domains.tenancy import require_repo_in_org
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1/repositories", tags=["attention"])


@router.get("/{repository_uid}/attention", operation_id="opensweep_repository_attention")
async def repository_attention(
    repository_uid: str,
    user: UserDTO = Depends(get_current_user),
) -> RepoAttention:
    await require_repo_in_org(repository_uid, user.org_uid)
    return await attention_service.repo_attention(repository_uid)
