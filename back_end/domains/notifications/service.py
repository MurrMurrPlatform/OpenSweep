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

Tenancy comes from domains/events/visibility.py, shared with the raw audit
route: callers see events for repositories in their org; platform-level events
(no repository) are instance-operator-only. Per-user read/dismiss state lives
in NotificationRead nodes; everything else is computed.

FEED_WINDOW bounds how far back a request looks, and the window is taken
inside the caller's scope rather than around it. Taking the newest 300 events
instance-wide and dropping other tenants' rows afterwards meant a busy
neighbour could push a small org's own events out of its inbox entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime

from neomodel import adb

from domains.events.models import Event
from domains.events.visibility import newest_first, visibility_scope, visible_clause
from domains.notifications.catalog import (
    BY_TYPE,
    CATEGORY_ATTENTION,
    CATEGORY_MENTIONS,
    RELEVANT_AUDIT_KINDS,
    category_for,
    event_types_for,
    kinds_for_category,
)
from domains.notifications.models import NotificationRead, read_state_key
from domains.notifications.schemas import NotificationCountsDTO, NotificationDTO
from domains.tenancy import org_repo_uids
from domains.users.schemas import UserDTO

# Newest catalog-relevant events considered per request — the inbox shows the
# recent past, not the full audit history (that stays on /audit).
FEED_WINDOW = 300


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


async def _recent_events(
    *,
    repos: list[str],
    platform_events: bool,
    kinds: frozenset[str],
    limit: int = FEED_WINDOW,
) -> list[Event]:
    """The newest `limit` catalog-relevant events the caller may see.

    Visibility is in the query, so the window counts only the caller's own
    events. Reading it the other way round — newest N instance-wide, then
    drop the other tenants — meant a busy neighbour could crowd a small org
    out of its own inbox entirely.
    """
    if not repos and not platform_events:
        return []
    rows, _ = await adb.cypher_query(
        f"MATCH (e:Event) WHERE e.kind IN $kinds "
        f"AND {visible_clause(platform_events=platform_events)} "
        f"RETURN e {newest_first()} LIMIT $limit",
        {"kinds": sorted(kinds), "repos": repos, "limit": limit},
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
    repos, platform_events = visibility_scope(
        allowed_repos=await org_repo_uids(user.org_uid),
        is_platform_admin=user.is_platform_admin,
        repository_uid=repository_uid,
    )
    # kinds_for_category is a superset — category_for still decides below.
    kinds = (
        RELEVANT_AUDIT_KINDS & kinds_for_category(category)
        if category
        else RELEVANT_AUDIT_KINDS
    )
    items: list[NotificationDTO] = []
    for event in await _recent_events(
        repos=repos, platform_events=platform_events, kinds=kinds
    ):
        item = to_item(event, user.uid)
        if item is None:
            continue
        if category and item.category != category:
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
