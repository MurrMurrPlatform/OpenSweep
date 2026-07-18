"""Notification DTOs."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class NotificationDTO(BaseModel):
    """One inbox item — an audit Event projected through the shared catalog,
    joined with the caller's read state."""

    uid: str  # the underlying Event uid
    kind: str
    category: str  # attention | activity | mentions
    label: str
    title: str = ""
    subject_type: str = ""
    subject_uid: str = ""
    repository_uid: str = ""
    payload: dict[str, Any] = {}
    occurred_at: datetime | None = None
    read_at: datetime | None = None


class NotificationCountsDTO(BaseModel):
    """Unread counts for the topbar bell badge."""

    total: int = 0
    attention: int = 0
    activity: int = 0
    mentions: int = 0
