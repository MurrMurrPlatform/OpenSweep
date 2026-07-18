"""Notification feed — visibility (mirrors audit tenancy, F3), mention
targeting, category/repository filters, read/dismiss state, counts, and the
route surface. DB-free: the event window and read-state fetchers are faked,
matching the style of test_audit_tenancy.py."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import api.v1.notifications as notifications_api
from domains.events.models import Event
from domains.notifications import service as feed
from domains.users.schemas import UserDTO

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


def _user(*, uid="u1", platform=False, org="org-a"):
    return UserDTO(
        uid=uid, email="e@x.y", display_name="U", role="admin",
        org_uid=org, org_role="owner", is_platform_admin=platform,
    )


def _event(uid, kind, *, repo="repo-a", payload=None, minutes_ago=0, subject_type="Run"):
    return Event(
        uid=uid,
        kind=kind,
        subject_uid=f"subj-{uid}",
        subject_type=subject_type,
        actor_uid="actor",
        repository_uid=repo,
        payload=payload or {},
        occurred_at=_NOW - timedelta(minutes=minutes_ago),
    )


@pytest.fixture
def harness(monkeypatch):
    events: list[Event] = []
    states: dict[str, SimpleNamespace] = {}

    async def fake_recent_events(limit=feed.FEED_WINDOW):
        return list(events)[:limit]

    async def fake_read_states(user_uid, event_uids):
        return {k: v for k, v in states.items() if k in event_uids}

    async def fake_org_repo_uids(org_uid):
        return {"repo-a"} if org_uid == "org-a" else set()

    monkeypatch.setattr(feed, "_recent_events", fake_recent_events)
    monkeypatch.setattr(feed, "_read_states", fake_read_states)
    monkeypatch.setattr(feed, "org_repo_uids", fake_org_repo_uids)
    return SimpleNamespace(events=events, states=states)


# ── projection (to_item) ─────────────────────────────────────────────────────


def test_irrelevant_kinds_project_to_nothing():
    assert feed.to_item(_event("e1", "provider.config_changed"), "u1") is None


def test_mention_events_target_only_the_mentioned_user():
    ev = _event("e1", "comment.mention", payload={"mentioned_user_uid": "u1"},
                subject_type="Comment")
    assert feed.to_item(ev, "u1") is not None
    assert feed.to_item(ev, "u2") is None


def test_needs_human_verdict_categorised_as_attention():
    ev = _event("e1", "verdict.submitted", payload={"result": "needs_human"})
    item = feed.to_item(ev, "u1")
    assert item is not None and item.category == "attention"


# ── feed listing ─────────────────────────────────────────────────────────────


async def test_feed_respects_audit_tenancy(harness):
    harness.events.append(_event("mine", "run.ended", repo="repo-a"))
    harness.events.append(_event("theirs", "run.ended", repo="repo-b"))
    harness.events.append(_event("platform", "repository.registered", repo=""))
    items = await feed.list_feed(_user())
    assert {i.uid for i in items} == {"mine"}


async def test_platform_admin_sees_platform_level_events(harness):
    harness.events.append(_event("platform", "repository.registered", repo=""))
    items = await feed.list_feed(_user(platform=True))
    assert {i.uid for i in items} == {"platform"}


async def test_category_and_repository_filters(harness):
    harness.events.append(_event("quota", "run.paused_quota"))
    harness.events.append(_event("done", "run.ended"))
    attention = await feed.list_feed(_user(), category="attention")
    assert {i.uid for i in attention} == {"quota"}
    none = await feed.list_feed(_user(), repository_uid="repo-zzz")
    assert none == []


async def test_read_and_dismiss_state(harness):
    harness.events.append(_event("seen", "run.ended"))
    harness.events.append(_event("gone", "run.ended"))
    harness.events.append(_event("new", "run.ended"))
    harness.states["seen"] = SimpleNamespace(read_at=_NOW, dismissed_at=None)
    harness.states["gone"] = SimpleNamespace(read_at=_NOW, dismissed_at=_NOW)
    everything = await feed.list_feed(_user())
    assert {i.uid for i in everything} == {"seen", "new"}  # dismissed never returns
    unread = await feed.list_feed(_user(), unread_only=True)
    assert {i.uid for i in unread} == {"new"}


async def test_unread_counts_by_category(harness):
    harness.events.append(_event("quota", "run.paused_quota"))
    harness.events.append(_event("done", "run.ended"))
    harness.events.append(
        _event("ping", "comment.mention",
               payload={"mentioned_user_uid": "u1"}, subject_type="Comment")
    )
    harness.events.append(
        _event("other", "comment.mention",
               payload={"mentioned_user_uid": "u2"}, subject_type="Comment")
    )
    counts = await feed.unread_counts(_user())
    assert counts.total == 3
    assert counts.attention == 1
    assert counts.activity == 1
    assert counts.mentions == 1


# ── mark-read / dismiss routes 404 cross-org (tenancy) ───────────────────────


@pytest.fixture
def route_fakes(monkeypatch, harness):
    class _Nodes:
        async def get_or_none(self, **kw):
            for e in harness.events:
                if e.uid == kw.get("uid"):
                    return e
            return None

    monkeypatch.setattr(
        notifications_api, "Event", SimpleNamespace(nodes=_Nodes())
    )

    async def fake_org_repo_uids(org_uid):
        return {"repo-a"} if org_uid == "org-a" else set()

    monkeypatch.setattr(notifications_api, "org_repo_uids", fake_org_repo_uids)

    marked: list[tuple[str, str]] = []

    async def fake_mark_read(user, uid):
        marked.append(("read", uid))

    async def fake_dismiss(user, uid):
        marked.append(("dismiss", uid))

    monkeypatch.setattr(feed, "mark_read", fake_mark_read)
    monkeypatch.setattr(feed, "dismiss", fake_dismiss)
    return marked


async def test_mark_read_404s_cross_org(harness, route_fakes):
    from fastapi import HTTPException

    harness.events.append(_event("theirs", "run.ended", repo="repo-b"))
    with pytest.raises(HTTPException) as exc:
        await notifications_api.mark_read("theirs", user=_user())
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await notifications_api.dismiss("missing", user=_user())
    assert exc.value.status_code == 404
    assert route_fakes == []


async def test_mark_read_marks_visible_events(harness, route_fakes):
    harness.events.append(_event("mine", "run.ended", repo="repo-a"))
    res = await notifications_api.mark_read("mine", user=_user())
    assert res == {"status": "read"}
    assert route_fakes == [("read", "mine")]


async def test_invalid_category_is_rejected(harness):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await notifications_api.list_notifications(
            category="everything", repository_uid=None, unread=False,
            limit=100, user=_user(),
        )
    assert exc.value.status_code == 422


# ── route surface ────────────────────────────────────────────────────────────


def test_notification_routes_are_mounted():
    from app import app

    paths = app.openapi()["paths"]
    assert "/api/v1/notifications" in paths
    assert "/api/v1/notifications/counts" in paths
    assert "/api/v1/notifications/read-all" in paths
    assert "/api/v1/notifications/{uid}/read" in paths
    assert "/api/v1/notifications/{uid}/dismiss" in paths
