"""Shared notification catalog — the single source of truth for Slack AND the
in-app inbox: the slack/events re-export stays identical, every audit kind
maps onto catalogued types, and each type carries a valid inbox category."""

from domains.notifications import catalog
from domains.notifications.catalog import (
    CATALOG,
    CATEGORIES,
    CATEGORY_ACTIVITY,
    CATEGORY_ATTENTION,
    CATEGORY_MENTIONS,
    EVENT_TYPES,
    RELEVANT_AUDIT_KINDS,
    category_for,
    event_types_for,
)

# ── the slack module re-exports the shared catalog (AC5) ─────────────────────


def test_slack_events_reexports_the_shared_catalog():
    from domains.slack import events as slack_events

    assert slack_events.CATALOG is catalog.CATALOG
    assert slack_events.BY_TYPE is catalog.BY_TYPE
    assert slack_events.EVENT_TYPES is catalog.EVENT_TYPES
    assert slack_events._KIND_MAP is catalog._KIND_MAP
    assert slack_events.RELEVANT_AUDIT_KINDS is catalog.RELEVANT_AUDIT_KINDS
    assert slack_events.event_types_for is catalog.event_types_for
    assert slack_events.NotificationEvent is catalog.NotificationEvent


# ── catalog integrity ────────────────────────────────────────────────────────


def test_catalog_types_unique_and_kind_map_closed():
    assert len({e.event_type for e in CATALOG}) == len(CATALOG)
    for kind in RELEVANT_AUDIT_KINDS:
        for event_type in event_types_for(kind, {}):
            assert event_type in EVENT_TYPES, (kind, event_type)


def test_every_type_carries_a_valid_category():
    for entry in CATALOG:
        assert entry.category in CATEGORIES, entry.event_type


def test_attention_and_mention_types():
    by_type = {e.event_type: e for e in CATALOG}
    assert by_type["attention.required"].category == CATEGORY_ATTENTION
    assert by_type["comment.mention"].category == CATEGORY_MENTIONS
    assert by_type["run.completed"].category == CATEGORY_ACTIVITY


def test_comment_mention_kind_is_relevant():
    assert "comment.mention" in RELEVANT_AUDIT_KINDS
    assert event_types_for("comment.mention", {}) == ["comment.mention"]


# ── category resolution ──────────────────────────────────────────────────────


def test_needs_human_verdict_lands_in_attention():
    types = event_types_for("verdict.submitted", {"result": "needs_human"})
    assert types == ["review.completed", "attention.required"]
    assert category_for(types) == CATEGORY_ATTENTION


def test_plain_verdict_stays_activity():
    types = event_types_for("verdict.submitted", {"result": "approve"})
    assert category_for(types) == CATEGORY_ACTIVITY


def test_paused_quota_is_attention():
    assert category_for(event_types_for("run.paused_quota", {})) == CATEGORY_ATTENTION


def test_mention_category():
    assert category_for(["comment.mention"]) == CATEGORY_MENTIONS


# ── read-state schema is bootstrapped ────────────────────────────────────────


def test_notification_read_constraint_is_bootstrapped():
    from infrastructure.neomodel_bootstrap import _CONSTRAINTS, _INDEXES

    assert any("NotificationRead" in c and "n.key IS UNIQUE" in c for c in _CONSTRAINTS)
    assert any("NotificationRead" in i and "n.user_uid" in i for i in _INDEXES)
