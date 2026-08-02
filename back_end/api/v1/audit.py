"""Audit log routes — list and get Events.

The Event nodes are written by `infrastructure/audit.py:write_audit()` on
every important tracking transition. This route exposes them read-only.

Tenancy: events carry repository_uid (derived from the subject at write
time). Callers see events for repositories in their org; events with no
repository (platform-level: provider/app config changes) are admin-only.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from neomodel import adb

from api.dependencies import get_current_user
from domains.events.models import Event
from domains.events.schemas import EventDTO
from domains.tenancy import org_repo_uids
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


def _to_dto(e: Event) -> EventDTO:
    return EventDTO(
        uid=e.uid,
        kind=e.kind,
        subject_uid=e.subject_uid,
        subject_type=e.subject_type,
        actor_uid=e.actor_uid,
        payload=dict(e.payload or {}),
        occurred_at=e.occurred_at or datetime.now(timezone.utc),
    )


def _visible(e: Event, allowed_repos: set[str], is_admin: bool) -> bool:
    repo = e.repository_uid or ""
    if repo:
        return repo in allowed_repos
    return is_admin  # platform-level event


def visibility_scope(
    *, allowed_repos: set[str], is_platform_admin: bool, repository_uid: str | None
) -> tuple[list[str], bool]:
    """What `list_events` may read: (repository uids, include platform events).

    The same rule `_visible` applies per node, hoisted so it can go into the
    query instead of filtering a full label scan afterwards.

    Platform-level events (no repository) are instance-operator-only. That
    MUST key off is_platform_admin, not the in-ORG capability role (F3):
    every personal-org owner is role="admin", so an org role would expose
    instance-wide events to any tenant.
    """
    if repository_uid is not None:
        # An explicit repo the caller cannot see returns nothing rather than
        # falling back to the whole org. It also never widens to platform
        # events — those belong to no repository.
        return ([repository_uid] if repository_uid in allowed_repos else [], False)
    return sorted(allowed_repos), is_platform_admin


@router.get("", response_model=list[EventDTO], operation_id="opensweep_list_audit_events")
async def list_events(
    subject_type: Optional[str] = Query(None),
    subject_uid: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    actor_uid: Optional[str] = Query(None),
    repository_uid: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: UserDTO = Depends(get_current_user),
):
    """Return the most recent Events in the caller's org, newest first.

    Filters are AND-combined. Platform-level events (no repository) appear
    for admins only.

    Visibility, ordering and the window are all one Cypher query: the audit
    log is the fastest-growing label in the graph, and reading it whole to
    hand back 100 rows got slower with every run the instance ever did.
    """
    repos, platform_events = visibility_scope(
        allowed_repos=await org_repo_uids(user.org_uid),
        is_platform_admin=user.is_platform_admin,
        repository_uid=repository_uid,
    )
    if not repos and not platform_events:
        return []

    visible = "e.repository_uid IN $repos"
    if platform_events:
        visible += " OR coalesce(e.repository_uid, '') = ''"
    where = [f"({visible})"]
    params: dict = {"repos": repos, "limit": limit, "offset": offset}
    for field, value in (
        ("subject_type", subject_type),
        ("subject_uid", subject_uid),
        ("kind", kind),
        ("actor_uid", actor_uid),
    ):
        if value:
            where.append(f"e.{field} = ${field}")
            params[field] = value
    rows, _ = await adb.cypher_query(
        f"MATCH (e:Event) WHERE {' AND '.join(where)} "
        "RETURN e ORDER BY e.occurred_at DESC SKIP $offset LIMIT $limit",
        params,
    )
    return [_to_dto(Event.inflate(row[0])) for row in rows]


@router.get("/{uid}", response_model=EventDTO, operation_id="opensweep_get_audit_event")
async def get_event(uid: str, user: UserDTO = Depends(get_current_user)):
    e = await Event.nodes.get_or_none(uid=uid)
    if e is None:
        raise HTTPException(status_code=404, detail=f"Event {uid} not found")
    allowed = await org_repo_uids(user.org_uid)
    if not _visible(e, allowed, user.is_platform_admin):  # is_platform_admin, not org role (F3)
        raise HTTPException(status_code=404, detail="not found")
    return _to_dto(e)
