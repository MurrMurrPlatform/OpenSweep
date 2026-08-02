"""The inbox feed window, against the real test Neo4j (localhost:7999).

The bug this pins: `_recent_events` took the newest FEED_WINDOW events
instance-wide and `list_feed` dropped the other tenants afterwards, so a busy
neighbour could push a small org's own events out of its inbox entirely — an
empty inbox with nothing wrong. The window is now applied inside the caller's
scope, which only a real query can demonstrate.

`test_notifications_feed.py` covers the projection and read-state logic with
an in-memory stand-in for this query; this file is here because that fake
cannot tell you whether the Cypher matches what it claims to.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from neomodel import adb

from domains.events.models import Event
from domains.notifications import service as feed
from domains.users.schemas import UserDTO

pytestmark = pytest.mark.integration


def _repo() -> str:
    return "repo-" + uuid4().hex[:8]


def _user(*, uid="u1", org="org-a", platform=False):
    return UserDTO(
        uid=uid, email="e@x.y", display_name="U", role="admin",
        org_uid=org, org_role="owner", is_platform_admin=platform,
    )


async def _event(repo: str, kind: str, *, payload=None, minutes_ago=0) -> Event:
    e = Event(
        uid=uuid4().hex,
        kind=kind,
        subject_uid="s",
        subject_type="Run",
        actor_uid="actor",
        repository_uid=repo,
        payload=payload or {},
        occurred_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )
    await e.save()
    return e


def _scope_to(monkeypatch, repos: set[str]) -> None:
    async def org_repos(org_uid):
        return set(repos)

    monkeypatch.setattr(feed, "org_repo_uids", org_repos)


async def test_a_noisy_neighbour_cannot_empty_a_small_orgs_inbox(monkeypatch):
    """The regression, reproduced: mine is older than a full window of theirs.

    Under the old query — newest FEED_WINDOW instance-wide, then drop other
    tenants in Python — `mine` fell outside the window and the inbox came
    back empty.
    """
    mine, theirs = _repo(), _repo()
    ours = await _event(mine, "run.ended", minutes_ago=10_000)  # long ago
    for i in range(feed.FEED_WINDOW + 20):  # a full window of newer noise
        await _event(theirs, "run.ended", minutes_ago=i)

    _scope_to(monkeypatch, {mine})
    items = await feed.list_feed(_user())
    assert [i.uid for i in items] == [ours.uid]


async def test_the_window_bounds_the_callers_own_events(monkeypatch):
    repo = _repo()
    for i in range(feed.FEED_WINDOW + 10):
        await _event(repo, "run.ended", minutes_ago=i)

    _scope_to(monkeypatch, {repo})
    items = await feed.list_feed(_user(), limit=feed.FEED_WINDOW + 50)
    assert len(items) == feed.FEED_WINDOW


async def test_other_tenants_events_never_appear(monkeypatch):
    mine, theirs = _repo(), _repo()
    ours = await _event(mine, "run.ended")
    await _event(theirs, "run.ended")

    _scope_to(monkeypatch, {mine})
    assert [i.uid for i in await feed.list_feed(_user())] == [ours.uid]


async def test_platform_events_reach_operators_only(monkeypatch):
    mine = _repo()
    await _event(mine, "run.ended")
    platform = await _event("", "repository.registered")

    _scope_to(monkeypatch, {mine})
    tenant = await feed.list_feed(_user())
    assert platform.uid not in {i.uid for i in tenant}
    operator = await feed.list_feed(_user(platform=True))
    assert platform.uid in {i.uid for i in operator}


async def test_a_category_filter_fills_the_window_with_that_category(monkeypatch):
    """The category filter is pushed into the query, so an attention item
    older than a window of chatter is still reachable — the same failure
    mode as the cross-tenant one, within a single org."""
    repo = _repo()
    urgent = await _event(repo, "run.paused_quota", minutes_ago=10_000)
    for i in range(feed.FEED_WINDOW + 20):
        await _event(repo, "run.ended", minutes_ago=i)

    _scope_to(monkeypatch, {repo})
    attention = await feed.list_feed(_user(), category="attention")
    assert [i.uid for i in attention] == [urgent.uid]


async def test_a_needs_human_verdict_shows_under_attention(monkeypatch):
    # The payload-dependent kind: the query narrows on kind, and category_for
    # still decides, so this must survive both.
    repo = _repo()
    needs_human = await _event(
        repo, "verdict.submitted", payload={"result": "needs_human"}
    )
    await _event(repo, "verdict.submitted", payload={"result": "approve"})

    _scope_to(monkeypatch, {repo})
    attention = await feed.list_feed(_user(), category="attention")
    assert [i.uid for i in attention] == [needs_human.uid]


async def test_undated_events_sort_last_not_first(monkeypatch):
    repo = _repo()
    newest = await _event(repo, "run.ended", minutes_ago=0)
    older = await _event(repo, "run.ended", minutes_ago=60)
    undated = await _event(repo, "run.ended")
    # occurred_at is default_now=True, so only raw Cypher can strip it.
    await adb.cypher_query(
        "MATCH (e:Event {uid: $uid}) REMOVE e.occurred_at", {"uid": undated.uid}
    )

    _scope_to(monkeypatch, {repo})
    items = await feed.list_feed(_user())
    assert [i.uid for i in items] == [newest.uid, older.uid, undated.uid]


async def test_a_repository_filter_outside_the_org_returns_nothing(monkeypatch):
    mine, theirs = _repo(), _repo()
    await _event(theirs, "run.ended")

    _scope_to(monkeypatch, {mine})
    assert await feed.list_feed(_user(), repository_uid=theirs) == []


async def test_a_caller_with_no_repos_reads_nothing(monkeypatch):
    await _event(_repo(), "run.ended")
    _scope_to(monkeypatch, set())
    assert await feed.list_feed(_user()) == []
