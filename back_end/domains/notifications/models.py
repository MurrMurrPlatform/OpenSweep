"""Per-user notification read state.

The inbox feed itself is DERIVED at read time from the audit Event stream
(domains/events/models.py) — no notification rows are fanned out on write.
The only stored state is per-user: whether one user has read or dismissed one
event.

Uniqueness on (user_uid, event_uid) is enforced through the single
unique-indexed `key` property ("{user_uid}:{event_uid}") because composite
unique constraints need Neo4j Enterprise — the same pattern used for
per-org Repository slugs (infrastructure/neomodel_bootstrap.py).
"""

from neomodel import (
    AsyncStructuredNode,
    DateTimeProperty,
    StringProperty,
)


def read_state_key(user_uid: str, event_uid: str) -> str:
    return f"{user_uid}:{event_uid}"


class NotificationRead(AsyncStructuredNode):
    # "{user_uid}:{event_uid}" — unique per (user, event).
    key = StringProperty(unique_index=True, required=True)
    user_uid = StringProperty(index=True, required=True)
    event_uid = StringProperty(index=True, required=True)
    # read_at set on mark-read; dismissed_at additionally removes the item
    # from the feed. Dismissed implies read.
    read_at = DateTimeProperty()
    dismissed_at = DateTimeProperty()
