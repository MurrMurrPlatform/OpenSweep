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


@router.get("", response_model=list[EventDTO], operation_id="opensweep_list_audit_events")
async def list_events(
    subject_type: Optional[str] = Query(None),
    subject_uid: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    actor_uid: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    user: UserDTO = Depends(get_current_user),
):
    """Return the most recent Events in the caller's org, newest first.

    Filters are AND-combined. Platform-level events (no repository) appear
    for admins only.
    """
    allowed = await org_repo_uids(user.org_uid)
    # Platform-level events (no repository) are instance-operator-only. This
    # MUST be is_platform_admin, not the in-ORG capability role (F3): every
    # personal-org owner is role="admin", so role_at_least would expose
    # instance-wide events to any tenant.
    is_admin = user.is_platform_admin
    nodes = await Event.nodes.all()
    out: list[EventDTO] = []
    for e in nodes:
        if not _visible(e, allowed, is_admin):
            continue
        if subject_type and e.subject_type != subject_type:
            continue
        if subject_uid and e.subject_uid != subject_uid:
            continue
        if kind and e.kind != kind:
            continue
        if actor_uid and e.actor_uid != actor_uid:
            continue
        out.append(_to_dto(e))
    out.sort(
        key=lambda x: x.occurred_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return out[:limit]


@router.get("/{uid}", response_model=EventDTO, operation_id="opensweep_get_audit_event")
async def get_event(uid: str, user: UserDTO = Depends(get_current_user)):
    e = await Event.nodes.get_or_none(uid=uid)
    if e is None:
        raise HTTPException(status_code=404, detail=f"Event {uid} not found")
    allowed = await org_repo_uids(user.org_uid)
    if not _visible(e, allowed, user.is_platform_admin):  # is_platform_admin, not org role (F3)
        raise HTTPException(status_code=404, detail="not found")
    return _to_dto(e)
