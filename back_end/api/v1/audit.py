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
from domains.events.visibility import (
    event_is_visible,
    newest_first,
    visibility_scope,
    visible_clause,
)
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


@router.get("", response_model=list[EventDTO], operation_id="opensweep_list_audit_events")
async def list_events(
    subject_type: Optional[str] = Query(None),
    subject_uid: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    actor_uid: Optional[str] = Query(None),
    repository_uid: Optional[str] = Query(None),
    # ge=0, not ge=1: limit=0 was always a legal (empty) page, but a negative
    # limit used to be a harmless Python slice and now goes into Cypher, which
    # rejects it outright.
    limit: int = Query(100, ge=0, le=500),
    offset: int = Query(0, ge=0),
    user: UserDTO = Depends(get_current_user),
):
    """Return the most recent Events in the caller's org, newest first.

    Filters are AND-combined. Platform-level events (no repository) appear
    for admins only.

    Visibility, ordering and the window are all one Cypher query: the audit
    log is the fastest-growing label in the graph, and reading it whole to
    hand back 100 rows got slower with every run the instance ever did.

    Unlike the other list routes this one sends no X-Total-Count: counting
    the matches means scanning them, which is the cost this route exists to
    avoid. Page until you get a short page.
    """
    repos, platform_events = visibility_scope(
        allowed_repos=await org_repo_uids(user.org_uid),
        is_platform_admin=user.is_platform_admin,
        repository_uid=repository_uid,
    )
    if not repos and not platform_events:
        return []

    where = [visible_clause(platform_events=platform_events)]
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
        f"RETURN e {newest_first()} SKIP $offset LIMIT $limit",
        params,
    )
    return [_to_dto(Event.inflate(row[0])) for row in rows]


@router.get("/{uid}", response_model=EventDTO, operation_id="opensweep_get_audit_event")
async def get_event(uid: str, user: UserDTO = Depends(get_current_user)):
    e = await Event.nodes.get_or_none(uid=uid)
    if e is None:
        raise HTTPException(status_code=404, detail=f"Event {uid} not found")
    allowed = await org_repo_uids(user.org_uid)
    if not event_is_visible(e, allowed, user.is_platform_admin):  # not the org role (F3)
        raise HTTPException(status_code=404, detail="not found")
    return _to_dto(e)
