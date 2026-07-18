"""Notification routes — the inbox / attention centre feed.

The feed is derived from the audit Event stream through the shared
notification catalog (domains/notifications/catalog.py); see
domains/notifications/service.py for the mechanism. These routes add the
per-user surface: list with filters, unread counts for the topbar bell, and
mark-read / dismiss / mark-all-read.

Tenancy: same visibility as api/v1/audit.py — repo-scoped events for the
caller's org, platform-level events for platform admins only. Cross-org
mark-read/dismiss 404s (existence never leaks, domains/tenancy.py).
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import get_current_user
from domains.events.models import Event
from domains.notifications import service as notification_service
from domains.notifications.catalog import CATEGORIES
from domains.notifications.schemas import NotificationCountsDTO, NotificationDTO
from domains.tenancy import org_repo_uids
from domains.users.schemas import UserDTO

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


def _require_category(category: Optional[str]) -> None:
    if category and category not in CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=f"category must be one of {', '.join(CATEGORIES)}",
        )


async def _require_visible_event(uid: str, user: UserDTO) -> Event:
    """The event, or 404 when it doesn't exist / isn't the caller's to see."""
    event = await Event.nodes.get_or_none(uid=uid)
    if event is None:
        raise HTTPException(status_code=404, detail="not found")
    allowed = await org_repo_uids(user.org_uid)
    if not notification_service._visible(event, allowed, user.is_platform_admin):
        raise HTTPException(status_code=404, detail="not found")
    return event


@router.get("", response_model=list[NotificationDTO], operation_id="opensweep_list_notifications")
async def list_notifications(
    category: Optional[str] = Query(None, description="attention | activity | mentions"),
    repository_uid: Optional[str] = Query(None),
    unread: bool = Query(False, description="only unread items"),
    limit: int = Query(100, le=300),
    user: UserDTO = Depends(get_current_user),
):
    """The caller's inbox, newest first. Dismissed items never return."""
    _require_category(category)
    return await notification_service.list_feed(
        user,
        category=category,
        repository_uid=repository_uid,
        unread_only=unread,
        limit=limit,
    )


@router.get(
    "/counts",
    response_model=NotificationCountsDTO,
    operation_id="opensweep_notification_counts",
)
async def notification_counts(user: UserDTO = Depends(get_current_user)):
    """Unread counts per inbox group — polled by the topbar bell."""
    return await notification_service.unread_counts(user)


@router.post("/read-all", operation_id="opensweep_notifications_mark_all_read")
async def mark_all_read(user: UserDTO = Depends(get_current_user)):
    marked = await notification_service.mark_all_read(user)
    return {"marked": marked}


@router.post("/{uid}/read", operation_id="opensweep_notification_mark_read")
async def mark_read(uid: str, user: UserDTO = Depends(get_current_user)):
    await _require_visible_event(uid, user)
    await notification_service.mark_read(user, uid)
    return {"status": "read"}


@router.post("/{uid}/dismiss", operation_id="opensweep_notification_dismiss")
async def dismiss(uid: str, user: UserDTO = Depends(get_current_user)):
    await _require_visible_event(uid, user)
    await notification_service.dismiss(user, uid)
    return {"status": "dismissed"}
