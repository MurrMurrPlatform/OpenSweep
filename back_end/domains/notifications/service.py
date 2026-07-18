"""In-app notification feed — the inbox / attention centre.

The feed is derived at read time from the audit Event stream
(infrastructure/audit.write_audit), which is already the single choke point
every state transition flows through — the same source Slack delivery
subscribes to. The shared catalog (domains/notifications/catalog.py) decides
which audit kinds are user-facing and which inbox group each lands in:

  - `attention` — attention.required event types (needs-human verdicts,
    verifications that could not conclude, quota-paused runs).
  - `mentions`  — comment.mention events, shown only to the mentioned user.
  - `activity`  — everything else user-facing.

Tenancy mirrors api/v1/audit.py `_visible`: callers see events for
repositories in their org; platform-level events (no repository) are
instance-operator-only. Per-user read/dismiss state lives in NotificationRead
nodes; everything else is computed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from neomodel import adb

from domains.events.models import Event
from domains.notifications.catalog import (
    BY_TYPE,
    CATEGORY_ATTENTION,
    CATEGORY_MENTIONS,
    RELEVANT_AUDIT_KINDS,
    category_for,
    event_types_for,
)
from domains.notifications.models import NotificationRead, read_state_key
from domains.notifications.schemas import NotificationCountsDTO, NotificationDTO
from domains.tenancy import org_repo_uids
from domains.users.schemas import UserDTO

# Newest catalog-relevant events considered per request — the inbox shows the
# recent past, not the full audit history (that stays on /audit).
FEED_WINDOW = 300


def _visible(event: Event, allowed_repos: set[str], is_admin: bool) -> bool:
    """Same semantics as api/v1/audit.py `_visible` (F3): repo-scoped events
    need the repo in the caller's org; platform-level events are
    instance-operator-only."""
    repo = event.repository_uid or ""
    if repo:
        return repo in allowed_repos
    return is_admin


def to_item(event: Event, user_uid: str) -> NotificationDTO | None:
    """Project one audit event into an inbox item, or None when it is not
    user-facing — including mention events aimed at someone else."""
    payload = dict(event.payload or {})
    types = event_types_for(event.kind, payload)
    if not types:
        return None
    if event.kind == "comment.mention" and payload.get("mentioned_user_uid") != user_uid:
        return None
    spec = BY_TYPE.get(types[0])
    return NotificationDTO(
        uid=event.uid,
        kind=event.kind,
        category=category_for(types),
        label=spec.label if spec else event.kind,
        title=str(payload.get("title") or ""),
        subject_type=event.subject_type or "",
        subject_uid=event.subject_uid or "",
        repository_uid=event.repository_uid or "",
        payload=payload,
        occurred_at=event.occurred_at,
    )


async def _recent_events(limit: int = FEED_WINDOW) -> list[Event]:
    rows, _ = await adb.cypher_query(
        "MATCH (e:Event) WHERE e.kind IN $kinds "
        "RETURN e ORDER BY e.occurred_at DESC LIMIT $limit",
        {"kinds": sorted(RELEVANT_AUDIT_KINDS), "limit": limit},
    )
    return [Event.inflate(row[0]) for row in rows]


async def _read_states(user_uid: str, event_uids: list[str]) -> dict[str, NotificationRead]:
    if not event_uids:
        return {}
    nodes = await NotificationRead.nodes.filter(user_uid=user_uid, event_uid__in=event_uids)
    return {n.event_uid: n for n in nodes}


async def list_feed(
    user: UserDTO,
    *,
    category: str | None = None,
    repository_uid: str | None = None,
    unread_only: bool = False,
    limit: int = 100,
) -> list[NotificationDTO]:
    """The caller's inbox, newest first. Dismissed items never return."""
    allowed = await org_repo_uids(user.org_uid)
    items: list[NotificationDTO] = []
    for event in await _recent_events():
        if not _visible(event, allowed, user.is_platform_admin):
            continue
        item = to_item(event, user.uid)
        if item is None:
            continue
        if category and item.category != category:
            continue
        if repository_uid and item.repository_uid != repository_uid:
            continue
        items.append(item)
    states = await _read_states(user.uid, [i.uid for i in items])
    out: list[NotificationDTO] = []
    for item in items:
        state = states.get(item.uid)
        if state is not None and state.dismissed_at:
            continue
        item.read_at = state.read_at if state is not None else None
        if unread_only and item.read_at:
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out


async def unread_counts(user: UserDTO) -> NotificationCountsDTO:
    """Unread (non-dismissed) counts per inbox group, for the bell badge."""
    items = await list_feed(user, unread_only=True, limit=FEED_WINDOW)
    counts = NotificationCountsDTO(total=len(items))
    for item in items:
        if item.category == CATEGORY_ATTENTION:
            counts.attention += 1
        elif item.category == CATEGORY_MENTIONS:
            counts.mentions += 1
        else:
            counts.activity += 1
    return counts


async def _mark(user_uid: str, event_uid: str, *, dismiss: bool = False) -> None:
    """Upsert the caller's read state for one event. Dismiss implies read."""
    key = read_state_key(user_uid, event_uid)
    node = await NotificationRead.nodes.get_or_none(key=key)
    if node is None:
        node = NotificationRead(key=key, user_uid=user_uid, event_uid=event_uid)
    now = datetime.now(UTC)
    node.read_at = node.read_at or now
    if dismiss:
        node.dismissed_at = node.dismissed_at or now
    try:
        await node.save()
    except Exception:
        # Unique-key race with a concurrent mark: re-read and re-apply.
        node = await NotificationRead.nodes.get(key=key)
        node.read_at = node.read_at or now
        if dismiss:
            node.dismissed_at = node.dismissed_at or now
        await node.save()


async def mark_read(user: UserDTO, event_uid: str) -> None:
    await _mark(user.uid, event_uid)


async def dismiss(user: UserDTO, event_uid: str) -> None:
    await _mark(user.uid, event_uid, dismiss=True)


async def mark_all_read(user: UserDTO) -> int:
    """Mark every currently-unread visible item read; returns how many."""
    items = await list_feed(user, unread_only=True, limit=FEED_WINDOW)
    for item in items:
        await _mark(user.uid, item.uid)
    return len(items)
