"""HTTP / MCP transport for the read-only OpenSweep-data tools.

Mirrors `api/v1/platform_tools.py` but for the read side: list/get/search
across Docs, Memories, Findings. These are required by the
look-before-write contract used in Generate-docs / Document / Audit runs.

Operation IDs are prefixed `opensweep_platform_read_*` so MCP can mount them
under the platform-tool surface (`/mcp/platform`) alongside the writers,
keeping the executor-facing surface in one place.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.dependencies import get_current_user
from api.platform_scope import require_tool_repo_access
from domains.findings.models import Finding
from domains.platform_tools.read_findings import (
    opensweep_get_finding,
    opensweep_list_findings,
    opensweep_search_findings,
)
from domains.platform_tools.docs_tools import list_docs as tool_list_docs
from domains.platform_tools.docs_tools import read_doc as tool_read_doc
from domains.platform_tools.memory_tools import search_memory as tool_search_memory
from domains.platform_tools.news_tools import list_interests as tool_list_interests
from domains.platform_tools.news_tools import list_news_items as tool_list_news_items
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1/platform-read", tags=["platform_read"])


# ---------- Docs + Memory (KNOWLEDGE_V3) ----------


@router.get(
    "/docs",
    operation_id="opensweep_platform_read_list_docs",
)
async def read_list_docs(
    request: Request,
    repository_uid: str = Query(...),
    user: UserDTO = Depends(get_current_user),
) -> list[dict[str, Any]]:
    await require_tool_repo_access(request, user, repository_uid)
    return await tool_list_docs(repository_uid=repository_uid)


@router.get(
    # `{slug:path}`, not `{slug}` — doc slugs are path-like
    # ("backend/queue-workers", doc_service.normalize_slug keeps the "/"), and
    # the default `str` convertor is `[^/]+`, so every folder page 404'd at the
    # router before the service was reached. FastAPI's OpenAPI path_format still
    # renders this as `/docs/{slug}`, which is the literal fastapi-mcp
    # substitutes into, so the generated tool keeps working unchanged.
    "/docs/{slug:path}",
    operation_id="opensweep_platform_read_doc",
)
async def read_doc(
    slug: str,
    request: Request,
    repository_uid: str = Query(...),
    user: UserDTO = Depends(get_current_user),
) -> dict[str, Any]:
    await require_tool_repo_access(request, user, repository_uid)
    return await tool_read_doc(repository_uid=repository_uid, slug=slug)


@router.get(
    "/memory-search",
    operation_id="opensweep_platform_read_search_memory",
)
async def read_search_memory(
    request: Request,
    repository_uid: str = Query(...),
    query: str = "",
    anchor_uid: str = "",
    limit: int = 10,
    user: UserDTO = Depends(get_current_user),
) -> list[dict[str, Any]]:
    await require_tool_repo_access(request, user, repository_uid)
    return await tool_search_memory(
        repository_uid=repository_uid, query=query, anchor_uid=anchor_uid, limit=limit
    )


# ---------- News radar (news-scout) ----------


@router.get(
    "/news-items",
    operation_id="opensweep_platform_read_list_news_items",
)
async def read_list_news_items(
    request: Request,
    repository_uid: str = Query(...),
    category: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    user: UserDTO = Depends(get_current_user),
) -> list[dict[str, Any]]:
    await require_tool_repo_access(request, user, repository_uid)
    return await tool_list_news_items(
        repository_uid=repository_uid, category=category, status=status, limit=limit
    )


@router.get(
    "/interests",
    operation_id="opensweep_platform_read_list_interests",
)
async def read_list_interests(
    request: Request,
    repository_uid: str = Query(...),
    enabled_only: bool = True,
    user: UserDTO = Depends(get_current_user),
) -> list[dict[str, Any]]:
    await require_tool_repo_access(request, user, repository_uid)
    return await tool_list_interests(
        repository_uid=repository_uid, enabled_only=enabled_only
    )


# ---------- Findings ----------


@router.get(
    "/findings",
    operation_id="opensweep_platform_read_list_findings",
)
async def list_findings(
    request: Request,
    repository_uid: str = Query(...),
    tag: Optional[str] = None,
    kind: Optional[str] = None,
    status: str = "open",
    limit: int = 100,
    user: UserDTO = Depends(get_current_user),
) -> list[dict[str, Any]]:
    await require_tool_repo_access(request, user, repository_uid)
    return await opensweep_list_findings(
        repository_uid=repository_uid,
        tag=tag,
        kind=kind,
        status=status,
        limit=limit,
    )


@router.get(
    "/findings/{uid}",
    operation_id="opensweep_platform_read_get_finding",
)
async def get_finding(
    uid: str, request: Request, user: UserDTO = Depends(get_current_user)
):
    finding = await Finding.nodes.get_or_none(uid=uid)
    if finding is None:
        raise HTTPException(status_code=404, detail="not found")
    await require_tool_repo_access(request, user, finding.repository_uid)
    return await opensweep_get_finding(uid=uid)


@router.get(
    "/findings-search",
    operation_id="opensweep_platform_read_search_findings",
)
async def search_findings(
    request: Request,
    repository_uid: str = Query(...),
    query: str = Query(...),
    status: str = "open",
    limit: int = 50,
    user: UserDTO = Depends(get_current_user),
) -> list[dict[str, Any]]:
    await require_tool_repo_access(request, user, repository_uid)
    return await opensweep_search_findings(
        repository_uid=repository_uid, query=query, status=status, limit=limit
    )
