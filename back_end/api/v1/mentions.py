"""Mention search — feeds the @-mention dropdown in the comment composer.

Org-scoped title search across every mentionable data-item type. One query
per requested type, capped per type: the dropdown wants a short, fresh list,
not pagination.
"""

from fastapi import APIRouter, Depends, Query
from neomodel import adb

from api.dependencies import get_current_user
from domains.comments.mentions import MENTIONABLE_TYPES
from domains.comments.schemas import MentionSearchResult
from domains.tenancy import org_repo_uids
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1/mentions", tags=["comments"])

_PER_TYPE_LIMIT = 8

# type → (node label, extra RETURN column rendered as the sublabel)
_TYPE_QUERIES: dict[str, tuple[str, str]] = {
    "ticket": ("Ticket", "n.status"),
    "finding": ("Finding", "n.severity"),
    "pull_request": ("PullRequest", "'#' + toString(n.github_number)"),
    "news_item": ("NewsItem", "n.category"),
    "run": ("Run", "n.playbook"),
    "scheduled_agent": ("ScheduledAgent", "n.title"),
    "doc": ("Doc", "n.slug"),
    "group": ("TicketGroupProposal", "n.status"),
}


@router.get(
    "/search",
    response_model=list[MentionSearchResult],
    operation_id="opensweep_mention_search",
)
async def search_mentions(
    q: str = Query("", max_length=200),
    types: str = Query("", description="comma-separated subset of mentionable types"),
    repository_uid: str = Query("", description="narrow to one repository"),
    user: UserDTO = Depends(get_current_user),
):
    """Data items matching `q` by title, newest first, grouped by type."""
    repo_uids = await org_repo_uids(user.org_uid)
    if repository_uid:
        repo_uids &= {repository_uid}
    if not repo_uids:
        return []

    requested = [t.strip() for t in types.split(",") if t.strip()] or list(_TYPE_QUERIES)
    out: list[MentionSearchResult] = []
    for kind in requested:
        if kind not in _TYPE_QUERIES or kind not in MENTIONABLE_TYPES:
            continue
        label, sublabel_expr = _TYPE_QUERIES[kind]
        rows, _ = await adb.cypher_query(
            f"""
            MATCH (n:{label})
            WHERE n.repository_uid IN $repos
              AND ($q = '' OR toLower(coalesce(n.title, '')) CONTAINS toLower($q))
            RETURN n.uid, coalesce(n.title, ''), coalesce({sublabel_expr}, ''),
                   n.repository_uid
            ORDER BY coalesce(n.updated_at, n.created_at) DESC
            LIMIT $limit
            """,
            {"repos": list(repo_uids), "q": q.strip(), "limit": _PER_TYPE_LIMIT},
        )
        out.extend(
            MentionSearchResult(
                type=kind,
                uid=row[0],
                label=row[1] or row[0],
                sublabel=str(row[2] or ""),
                repository_uid=row[3] or "",
            )
            for row in rows
        )
    return out
