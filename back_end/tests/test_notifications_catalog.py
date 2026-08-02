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


def test_campaign_outcomes_are_catalogued():
    assert event_types_for("campaign.completed", {}) == ["campaign.completed"]
    assert category_for(["campaign.completed"]) == CATEGORY_ACTIVITY
    assert event_types_for("campaign.failed", {}) == ["campaign.failed"]
    assert category_for(["campaign.failed"]) == CATEGORY_ATTENTION


def test_normal_run_completion_notifies():
    # Every self-completed run lands as `run.awaiting_input` (complete_run
    # aliases the agent's "completed" onto it) — that is the everyday "run
    # finished" signal, not run.ended (a human closing the run).
    assert "run.awaiting_input" in RELEVANT_AUDIT_KINDS
    types = event_types_for("run.awaiting_input", {})
    assert types == ["run.completed"]
    assert category_for(types) == CATEGORY_ACTIVITY


def test_limit_exceeded_is_attention():
    types = event_types_for("run.limit_exceeded", {})
    assert types == ["attention.required"]
    assert category_for(types) == CATEGORY_ATTENTION


def test_agent_question_is_attention():
    # ask_user (thread session agent) — the run is literally waiting on a
    # human answer.
    types = event_types_for("thread.question_asked", {})
    assert types == ["attention.required"]
    assert category_for(types) == CATEGORY_ATTENTION


def test_mention_category():
    assert category_for(["comment.mention"]) == CATEGORY_MENTIONS


# ── read-state schema is bootstrapped ────────────────────────────────────────


def test_notification_read_constraint_is_bootstrapped():
    from infrastructure.neomodel_bootstrap import _CONSTRAINTS, _INDEXES

    assert any("NotificationRead" in c and "n.key IS UNIQUE" in c for c in _CONSTRAINTS)
    assert any("NotificationRead" in i and "n.user_uid" in i for i in _INDEXES)


# ── kinds_for_category vs the real categoriser ───────────────────────────────


def test_kinds_for_category_never_omits_a_kind_that_can_land_there():
    """The category filter narrows the feed query; a miss silently loses items.

    `kinds_for_category` is derived separately from the payload promotions in
    `event_types_for` (see _PAYLOAD_CONDITIONAL), so the two can drift. This
    replays every kind through the real categoriser — with and without the
    needs-human payload that promotes a verdict to attention — and demands
    the resulting category be claimed for that kind.
    """
    from domains.notifications.catalog import (
        CATEGORIES,
        RELEVANT_AUDIT_KINDS,
        category_for,
        event_types_for,
        kinds_for_category,
    )

    claimed = {c: kinds_for_category(c) for c in CATEGORIES}
    for kind in RELEVANT_AUDIT_KINDS:
        for payload in ({}, {"result": "needs_human"}):
            types = event_types_for(kind, payload)
            if not types:
                continue
            category = category_for(types)
            assert kind in claimed[category], (
                f"{kind} resolves to {category!r} with payload {payload} "
                f"but kinds_for_category({category!r}) omits it"
            )


def test_kinds_for_category_covers_every_relevant_kind():
    # Union over categories must lose nothing: an unfiltered feed and the
    # three filtered ones should between them reach every relevant kind.
    from domains.notifications.catalog import (
        CATEGORIES,
        RELEVANT_AUDIT_KINDS,
        kinds_for_category,
    )

    covered = set().union(*(kinds_for_category(c) for c in CATEGORIES))
    assert covered == set(RELEVANT_AUDIT_KINDS)


def test_a_needs_human_verdict_is_reachable_from_both_categories():
    # The one payload-dependent kind: it must be claimed by attention (so the
    # attention tab shows it) AND activity (it is still a completed review).
    from domains.notifications.catalog import (
        CATEGORY_ACTIVITY,
        CATEGORY_ATTENTION,
        kinds_for_category,
    )

    assert "verdict.submitted" in kinds_for_category(CATEGORY_ATTENTION)
    assert "verdict.submitted" in kinds_for_category(CATEGORY_ACTIVITY)
