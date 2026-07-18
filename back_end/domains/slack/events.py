"""Slack notification event catalog — re-exported from the shared
domains/notifications/catalog module.

The catalog moved so the in-app inbox (domains/notifications) and Slack
delivery derive from ONE source of truth: a single catalog change updates
both surfaces. This module keeps the historical import path for Slack code
and tests.
"""

from domains.notifications.catalog import (  # noqa: F401
    _KIND_MAP,
    BY_TYPE,
    CATALOG,
    EVENT_TYPES,
    RELEVANT_AUDIT_KINDS,
    NotificationEvent,
    event_types_for,
)

__all__ = [
    "_KIND_MAP",
    "BY_TYPE",
    "CATALOG",
    "EVENT_TYPES",
    "RELEVANT_AUDIT_KINDS",
    "NotificationEvent",
    "event_types_for",
]
